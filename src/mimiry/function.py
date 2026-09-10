"""``@mimiry.function`` decorator and the ``Function`` runtime class.

``.remote()`` creates a fresh Mimiry session, runs the user's function inside
it, and connects over SSH to fetch the serialized return value. ``.map()``
creates one session and streams every item through it: the cold start
(provision, boot, image pull — five to eight minutes today) is paid once, not
per item, and an item that fails does not discard the ones that finished.
There is no warm pool.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from mimiry._auth import get_token
from mimiry._availability import preflight_gpu_availability
from mimiry._client import MimiryClient
from mimiry._config import get_config
from mimiry._serialization import (
    RemoteFunctionError,
    build_bootstrap_script,
    caller_python_env_var,
    caller_python_version,
    new_result_hmac_key,
    pack_args,
    pack_call,
    pack_fn,
    parse_result,
    payload_env_var,
    result_hmac_env_var,
    verify_result_envelope,
)
from mimiry._session import (
    TERMINAL_STATES,
    RunInfo,
    build_run_info,
    make_terminal_check,
    preflight_volume_location,
    raise_if_ended_before_result,
    raise_if_failed,
    wait_for_ssh_ready,
    wait_for_started_or_terminal,
)
from mimiry._ssh import (
    CALLS_DIR,
    RESULT_FILE,
    RESULTS_DIR,
    SSHError,
    SshTarget,
    close_control_channel,
    fetch_remote_file,
    open_control_channel,
    push_remote_file,
    signal_done,
    ssh_target_from_session,
    wait_for_remote_file,
    wait_for_sshd,
)
from mimiry.exceptions import MapError, ResultIntegrityError, ResultParseError, SessionError
from mimiry.image import Image, normalize_image, preflight_python_version


def _public_key(ssh_key_path: Path) -> str:
    pub = Path(f"{ssh_key_path}.pub")
    return pub.read_text().strip()


def _session_name(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"


# Defaults every provider on the platform can satisfy today. "A100" is a
# family alias resolved to a concrete catalog name by the availability
# preflight; a bare ``@mimiry.function()`` must be able to succeed.
DEFAULT_GPU = "A100"
DEFAULT_IMAGE = "nvcr.io/nvidia/pytorch:24.01-py3"


@dataclass
class FunctionConfig:
    gpu: str = DEFAULT_GPU
    gpu_count: int = 1
    # The default image must pull without credentials on the platform; NGC's
    # ``nvidia/cuda`` images do not, ``nvidia/pytorch`` does. It ships Python
    # 3.10, and cloudpickle payloads only load on the same Python minor as the
    # caller, so a caller on another version must pick an image that matches.
    # See README "Python version".
    image: Image | str = DEFAULT_IMAGE
    timeout_seconds: int | None = None  # falls back to config.timeout_seconds
    provider: str | None = None
    location: str | None = None
    environment_vars: dict[str, str] = field(default_factory=dict)
    name_prefix: str | None = None
    # ``{"volume-name": "/mount/path"}``. A volume lives in one location; the
    # session adopts it (or is refused before it exists if ``location``
    # disagrees).
    volumes: dict[str, str] = field(default_factory=dict)


class Function:
    """A user function bound to GPU/image config. Created via ``@mimiry.function``."""

    def __init__(self, fn: Callable, cfg: FunctionConfig) -> None:
        self._fn = fn
        self._cfg = cfg
        self.__name__ = getattr(fn, "__name__", "function")
        self.__doc__ = fn.__doc__
        #: Session id, GPU, rate, per-phase seconds and settled cost of the
        #: most recent ``.remote()`` / ``.map()``; ``None`` before the first.
        self.last_run: RunInfo | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(*args, **kwargs)

    def local(self, *args: Any, **kwargs: Any) -> Any:
        """Run the function in the local Python process (skips Mimiry entirely)."""
        return self._fn(*args, **kwargs)

    def remote(self, *args: Any, **kwargs: Any) -> Any:
        """Run the function on a Mimiry GPU session. Blocks until done. Returns
        the value. ``self.last_run`` afterwards says what it cost."""
        try:
            result, self.last_run = _run_remote(self._fn, self._cfg, args, kwargs)
        except SessionError as e:
            # A failed call still cost a session; leave its figures reachable.
            self.last_run = getattr(e, "run", None)
            raise
        return result

    def map(self, iterable: Iterable[Any], *, kwargs_list: list[dict] | None = None) -> list:
        """Apply the function to every item on one GPU session and return the
        results in order.

        The session is created once and each item is streamed through it, so
        the cold start is paid once. Items run one after another on that
        session. If an item's call raises inside the container, the exception
        is collected and the rest still run; when any failed, :class:`MapError`
        is raised at the end carrying every successful result and every
        failure, so finished work is never thrown away.
        """
        items = list(iterable)
        kwargs_list = kwargs_list or [{} for _ in items]
        if len(kwargs_list) != len(items):
            raise ValueError("kwargs_list length must match iterable length")
        if not items:
            return []
        calls = [((item,), kw) for item, kw in zip(items, kwargs_list, strict=True)]
        try:
            results, self.last_run = _run_map(self._fn, self._cfg, calls)
        except SessionError as e:  # MapError included
            self.last_run = getattr(e, "run", None)
            raise
        return results


def function(
    *,
    gpu: str = DEFAULT_GPU,
    gpu_count: int = 1,
    # The default image must pull without credentials on the platform; NGC's
    # ``nvidia/cuda`` images do not, ``nvidia/pytorch`` does. It ships Python
    # 3.10, and cloudpickle payloads only load on the same Python minor as the
    # caller, so a caller on another version must pick an image that matches.
    # See README "Python version".
    image: Image | str = DEFAULT_IMAGE,
    timeout: int | None = None,
    provider: str | None = None,
    location: str | None = None,
    env: dict[str, str] | None = None,
    name: str | None = None,
    volume: str | dict[str, str] | None = None,
) -> Callable[[Callable], Function]:
    """Decorator: turn a Python function into a Mimiry-remote callable.

    ``volume`` attaches a persistent volume created with ``mimiry volume
    create``: a name mounts it at ``/data``; a ``{name: path}`` mapping picks
    the mount points. What the function writes there is still there for the
    next call.

    Example::

        @mimiry.function(gpu="A100", image="nvcr.io/nvidia/pytorch:24.01-py3", volume="ckpt")
        def train(dataset: str) -> dict:
            import torch
            torch.save({"loss": 0.1}, "/data/last.pt")
            return {"loss": 0.1}

        train.remote("imagenet-small")
    """
    if volume is None:
        volumes: dict[str, str] = {}
    elif isinstance(volume, str):
        volumes = {volume: "/data"}
    else:
        volumes = dict(volume)
    cfg = FunctionConfig(
        gpu=gpu,
        gpu_count=gpu_count,
        image=image,
        timeout_seconds=timeout,
        provider=provider,
        location=location,
        environment_vars=env or {},
        name_prefix=name,
        volumes=volumes,
    )

    def wrap(fn: Callable) -> Function:
        if cfg.name_prefix is None:
            cfg.name_prefix = getattr(fn, "__name__", "mimiry-fn")
        return Function(fn, cfg)

    return wrap


def _build_session_payload(cfg: FunctionConfig, command: str, env_vars: dict[str, str]) -> dict:
    config = get_config()
    if config.ssh_key_path is None:
        raise RuntimeError(
            "Mimiry SDK is not configured. Set MIMIRY_SSH_KEY or call mimiry.configure(...)."
        )

    image = normalize_image(cfg.image)
    pub_key = _public_key(config.ssh_key_path)

    gpu_spec: dict[str, Any] = {"types": [cfg.gpu], "count": cfg.gpu_count}
    if cfg.provider is not None:
        gpu_spec["provider"] = cfg.provider
    if cfg.location is not None:
        gpu_spec["location"] = cfg.location

    merged_env = {**image.env_vars, **cfg.environment_vars, **env_vars}

    payload = {
        "name": _session_name(cfg.name_prefix or "mimiry-fn"),
        "image": {"uri": image.uri},
        "gpu": gpu_spec,
        "command": command,
        "environment_vars": merged_env,
        # SSH is how the result comes back — see _ssh.py for the rationale.
        "ssh_enabled": True,
        "ssh_public_key": pub_key,
        "auto_terminate": {"mode": "on_complete"},
    }
    if cfg.volumes:
        payload["volume_mounts"] = [
            {"volume_name": name, "mount_path": path} for name, path in cfg.volumes.items()
        ]
    return payload


@dataclass
class _Attached:
    """A live session with an open SSH control channel, ready for commands."""

    session_id: str
    target: SshTarget
    terminal_check: Callable[[], str | None]
    hmac_key: str
    timeout: int
    started_at: float = 0.0
    phases: dict[str, float] = field(default_factory=dict)


def _log(msg: str) -> None:
    if os.environ.get("MIMIRY_VERBOSE", "1") != "0":
        print(f"[mimiry] {msg}", file=sys.stderr, flush=True)


def _prepare(fn: Callable, cfg: FunctionConfig, *, worker: bool) -> tuple[dict, str, int, Any]:
    """Everything that happens before a session exists: config, the Python
    preflight, packing, and the session payload. Returns
    ``(payload, hmac_key, timeout, config)``. Nothing here costs money.
    """
    config = get_config()
    timeout = cfg.timeout_seconds or config.timeout_seconds
    if config.ssh_key_path is None:
        raise RuntimeError(
            "Mimiry SDK is not configured. Set MIMIRY_SSH_KEY or call mimiry.configure(...)."
        )
    image = normalize_image(cfg.image)
    # Before anything is created or charged: a caller Python that the image's
    # declared Python can't load is a guaranteed crash on arrival.
    caller_py = caller_python_version()
    preflight_python_version(image, caller_py)
    hmac_key = new_result_hmac_key()
    command = build_bootstrap_script(image_install_prefix=image.install_prefix(), worker=worker)
    env_vars = {
        result_hmac_env_var(): hmac_key,
        # The container re-checks this against its own interpreter, so an
        # undeclared image still fails with an explanation rather than a
        # segfault the SDK would misreport as an SSH problem.
        caller_python_env_var(): caller_py,
    }
    if worker:
        env_vars[payload_env_var()] = pack_fn(fn)
    return _build_session_payload(cfg, command, env_vars), hmac_key, timeout, config


def _attach(
    client: MimiryClient,
    cfg: FunctionConfig,
    payload: dict,
    hmac_key: str,
    timeout: int,
    config: Any,
    created: dict | None = None,
) -> _Attached:
    """Create the session and bring it to the point where SSH commands work.
    Raises with the container's own diagnosis if it dies on the way.
    ``created``, if given, receives ``{"id": ..., "started_at": ...}`` as soon
    as the session exists, so a caller can still account for it when the
    attach fails afterwards."""
    run_config = type(config)(
        ssh_key_path=config.ssh_key_path,
        api_base=config.api_base,
        timeout_seconds=timeout,
        poll_interval_seconds=config.poll_interval_seconds,
        log_poll_interval_seconds=config.log_poll_interval_seconds,
    )
    # A volume lives in one location; settle that before the GPU check so the
    # check runs against the location the session will really use.
    location = preflight_volume_location(
        client, payload.get("volume_mounts") or [], cfg.location
    ) or cfg.location
    if location:
        payload["gpu"]["location"] = location
    # Fail fast on an impossible gpu/provider combo before paying for a
    # provisioning round-trip, and resolve a GPU family alias (e.g. "A100") to
    # the concrete catalog names the API requires. Best-effort — a flaky
    # availability endpoint won't block submission. See _availability.py.
    payload["gpu"]["types"] = preflight_gpu_availability(client, cfg.gpu, cfg.provider, location)

    started_at = time.monotonic()
    session = client.create_session(payload)
    session_id = session["id"]
    if created is not None:
        created["id"] = session_id
        created["started_at"] = started_at
    _log(f"session {session_id} submitted")

    ran_payload, phases = wait_for_started_or_terminal(
        client, session_id, run_config, on_state_change=lambda st: _log(f"state={st}")
    )
    # If the container ended before we could attach, surface its logs now
    # instead of blundering into a 300s SSH timeout.
    raise_if_ended_before_result(ran_payload, client=client)

    _log("waiting for ssh.host to be populated")
    ssh_ready = wait_for_ssh_ready(client, session_id, run_config)
    raise_if_failed(ssh_ready, client=client)

    target = ssh_target_from_session(ssh_ready, config.ssh_key_path)
    _log(f"sshing into {target.host}:{target.port}")
    terminal_check = make_terminal_check(client, session_id)
    try:
        wait_for_sshd(target, terminal_check=terminal_check)
    except SSHError:
        # If the box is gone because the container died, the container's
        # own logs say why — a transport error would send the user
        # debugging their network instead.
        raise_if_ended_before_result(client.get_session(session_id), client=client)
        raise

    _log("opening SSH control channel (multiplexing for the bootstrap install storm)")
    target = open_control_channel(target)
    return _Attached(session_id, target, terminal_check, hmac_key, timeout, started_at, phases)


def _release(
    client: MimiryClient,
    att: _Attached | None,
    session_id: str | None,
    started_at: float | None = None,
) -> RunInfo | None:
    """Best-effort teardown: tell the container we are done, close the
    channel, terminate anything still alive, then read what it cost. Never
    raises; returns ``None`` only when no session was ever created."""
    if att is not None:
        try:
            signal_done(att.target)
        except Exception as e:
            _log(f"warning: signal_done failed ({e}); container will time out on its own")
        close_control_channel(att.target)
    if session_id is None:
        return None
    try:
        state = (client.get_session(session_id).get("state") or "").lower()
        if state not in TERMINAL_STATES:
            client.terminate_session(session_id)
    except Exception:
        pass
    return build_run_info(
        client,
        session_id,
        phases=att.phases if att is not None else {},
        started_at=att.started_at if att is not None else (started_at or time.monotonic()),
    )


def _decode(raw: str, hmac_key: str, session_id: str) -> Any:
    """Verify the result's HMAC, then deserialize. A remote exception is
    re-raised here as :class:`RemoteFunctionError`."""
    try:
        return parse_result(verify_result_envelope(raw, hmac_key))
    except ResultIntegrityError as e:
        raise ResultIntegrityError(f"{e} (session {session_id})") from e
    except ResultParseError as e:
        raise ResultParseError(f"{e} (session {session_id})") from e


def _run_remote(fn: Callable, cfg: FunctionConfig, args: tuple, kwargs: dict) -> tuple[Any, RunInfo | None]:
    """Internal: do one end-to-end remote call on a fresh session. Returns
    ``(result, run_info)``."""
    payload, hmac_key, timeout, config = _prepare(fn, cfg, worker=False)
    payload["environment_vars"][payload_env_var()] = pack_call(fn, args, kwargs)
    token = get_token(config.ssh_key_path, config.api_base)

    with MimiryClient(token) as client:
        att: _Attached | None = None
        created: dict = {}
        session_id: str | None = None
        try:
            att = _attach(client, cfg, payload, hmac_key, timeout, config, created)
            session_id = att.session_id
            _log(f"waiting for {RESULT_FILE}")
            wait_for_remote_file(
                att.target, RESULT_FILE, max_wait_seconds=timeout, terminal_check=att.terminal_check
            )
            _log("fetching result")
            raw = fetch_remote_file(att.target, RESULT_FILE).decode("utf-8", errors="replace")
            result = _decode(raw, hmac_key, session_id)
        except Exception as e:
            session_id = session_id or created.get("id")
            _narrate_failure(client, session_id)
            info = _release(client, att, session_id, created.get("started_at"))
            att = None
            session_id = None
            _log_cost(info)
            if isinstance(e, SessionError):
                e.run = info
            raise
        finally:
            if session_id is not None:
                info = _release(client, att, session_id)
                _log_cost(info)
        return result, info


def _run_map(fn: Callable, cfg: FunctionConfig, calls: list[tuple[tuple, dict]]) -> tuple[list, RunInfo | None]:
    """Internal: one session, every call streamed through it in order.
    Returns ``(results, run_info)``; a :class:`MapError` carries the info."""
    payload, hmac_key, timeout, config = _prepare(fn, cfg, worker=True)
    token = get_token(config.ssh_key_path, config.api_base)

    results: list[Any] = []
    failures: list[tuple[int, BaseException]] = []
    with MimiryClient(token) as client:
        att: _Attached | None = None
        created: dict = {}
        session_id: str | None = None
        try:
            att = _attach(client, cfg, payload, hmac_key, timeout, config, created)
            session_id = att.session_id
            for n, (args, kwargs) in enumerate(calls):
                _log(f"map: item {n + 1}/{len(calls)}")
                push_remote_file(att.target, f"{CALLS_DIR}/{n}.b64", pack_args(args, kwargs))
                result_path = f"{RESULTS_DIR}/{n}.b64"
                wait_for_remote_file(
                    att.target,
                    result_path,
                    max_wait_seconds=timeout,
                    poll_interval=1.0,
                    terminal_check=att.terminal_check,
                )
                raw = fetch_remote_file(att.target, result_path).decode("utf-8", errors="replace")
                try:
                    results.append(_decode(raw, hmac_key, session_id))
                except RemoteFunctionError as e:
                    # The container is still up and the next item will run;
                    # keep the failure and carry on.
                    results.append(None)
                    failures.append((n, e))
        except Exception as e:
            # The session itself is gone (capacity, container death, SSH). Do
            # not discard what already came back.
            session_id = session_id or created.get("id")
            _narrate_failure(client, session_id)
            info = _release(client, att, session_id, created.get("started_at"))
            att = None
            session_id = None
            _log_cost(info)
            if isinstance(e, SessionError):
                e.run = info
            if results:
                raise MapError(
                    f"map stopped after {len(results)} of {len(calls)} items: {e}",
                    results=results + [None] * (len(calls) - len(results)),
                    failures=failures + [(len(results), e)],
                    total=len(calls),
                    run=info,
                ) from e
            raise
        finally:
            if session_id is not None:
                info = _release(client, att, session_id)
                _log_cost(info)

    if failures:
        raise MapError(
            f"{len(failures)} of {len(calls)} map items raised inside the container",
            results=results,
            failures=failures,
            total=len(calls),
            run=info,
        )
    return results, info


def _log_cost(info: RunInfo | None) -> None:
    if info is None:
        return
    cost = f"{info.final_cost:.4f} {info.currency or ''}".strip() if info.final_cost is not None else "not settled yet"
    dur = f"{info.duration:.0f}s" if info.duration is not None else "?"
    _log(f"session {info.session_id}: {info.gpu_type or '?'} for {dur}, cost {cost}")


def _narrate_failure(client: MimiryClient, session_id: str | None) -> None:
    if session_id is None:
        return
    try:
        final = client.get_session(session_id, events_tail=-1)
        _log(
            f"final session payload: state={final.get('state')} "
            f"stop_reason={final.get('stop_reason')} error={final.get('error')}"
        )
    except Exception:
        pass


__all__ = ["Function", "FunctionConfig", "MapError", "RunInfo", "SessionError", "function"]
