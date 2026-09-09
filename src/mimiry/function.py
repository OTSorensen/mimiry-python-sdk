"""``@mimiry.function`` decorator and the ``Function`` runtime class.

v1 contract: every ``.remote()`` call creates a fresh Mimiry session, runs the
user's function inside it, and connects over SSH to fetch the serialized return
value. Cold-start is ~2 minutes. There is no warm pool in v1.
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
    build_bootstrap_script,
    caller_python_env_var,
    caller_python_version,
    new_result_hmac_key,
    pack_call,
    parse_result,
    payload_env_var,
    result_hmac_env_var,
    verify_result_envelope,
)
from mimiry._session import (
    TERMINAL_STATES,
    make_terminal_check,
    raise_if_ended_before_result,
    raise_if_failed,
    wait_for_ssh_ready,
    wait_for_started_or_terminal,
)
from mimiry._ssh import (
    RESULT_FILE,
    SSHError,
    close_control_channel,
    fetch_remote_file,
    open_control_channel,
    signal_done,
    ssh_target_from_session,
    wait_for_remote_file,
    wait_for_sshd,
)
from mimiry.exceptions import ResultIntegrityError, ResultParseError, SessionError
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


class Function:
    """A user function bound to GPU/image config. Created via ``@mimiry.function``."""

    def __init__(self, fn: Callable, cfg: FunctionConfig) -> None:
        self._fn = fn
        self._cfg = cfg
        self.__name__ = getattr(fn, "__name__", "function")
        self.__doc__ = fn.__doc__

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(*args, **kwargs)

    def local(self, *args: Any, **kwargs: Any) -> Any:
        """Run the function in the local Python process (skips Mimiry entirely)."""
        return self._fn(*args, **kwargs)

    def remote(self, *args: Any, **kwargs: Any) -> Any:
        """Run the function on a Mimiry GPU session. Blocks until done. Returns the value."""
        return _run_remote(self._fn, self._cfg, args, kwargs)

    def map(self, iterable: Iterable[Any], *, kwargs_list: list[dict] | None = None) -> list:
        """Sequentially apply the function across an iterable.

        v1 limitation: Mimiry caps users at 2 concurrent sessions, so ``map`` runs
        items one at a time. v2 backend changes will lift this.
        """
        items = list(iterable)
        kwargs_list = kwargs_list or [{} for _ in items]
        if len(kwargs_list) != len(items):
            raise ValueError("kwargs_list length must match iterable length")
        return [_run_remote(self._fn, self._cfg, (item,), kw) for item, kw in zip(items, kwargs_list)]


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
) -> Callable[[Callable], Function]:
    """Decorator: turn a Python function into a Mimiry-remote callable.

    Example::

        @mimiry.function(gpu="A100", provider="verda", image="nvcr.io/nvidia/pytorch:24.01-py3")
        def train(dataset: str) -> dict:
            import torch
            return {"loss": 0.1}

        train.remote("imagenet-small")
    """
    cfg = FunctionConfig(
        gpu=gpu,
        gpu_count=gpu_count,
        image=image,
        timeout_seconds=timeout,
        provider=provider,
        location=location,
        environment_vars=env or {},
        name_prefix=name,
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

    return {
        "name": _session_name(cfg.name_prefix or "mimiry-fn"),
        "image": {"uri": image.uri},
        "gpu": gpu_spec,
        "command": command,
        "environment_vars": merged_env,
        # v1 requires SSH for result retrieval — see _ssh.py for the rationale.
        "ssh_enabled": True,
        "ssh_public_key": pub_key,
        "auto_terminate": {"mode": "on_complete"},
    }


def _run_remote(fn: Callable, cfg: FunctionConfig, args: tuple, kwargs: dict) -> Any:
    """Internal: do one end-to-end remote call."""
    config = get_config()
    timeout = cfg.timeout_seconds or config.timeout_seconds

    if config.ssh_key_path is None:
        raise RuntimeError(
            "Mimiry SDK is not configured. Set MIMIRY_SSH_KEY or call mimiry.configure(...)."
        )

    token = get_token(config.ssh_key_path, config.api_base)
    image = normalize_image(cfg.image)
    # Before anything is created or charged: a caller Python that the image's
    # declared Python can't load is a guaranteed crash on arrival.
    caller_py = caller_python_version()
    preflight_python_version(image, caller_py)
    payload_b64 = pack_call(fn, args, kwargs)
    hmac_key = new_result_hmac_key()
    command = build_bootstrap_script(image_install_prefix=image.install_prefix())
    env_vars = {
        payload_env_var(): payload_b64,
        result_hmac_env_var(): hmac_key,
        # The container re-checks this against its own interpreter, so an
        # undeclared image still fails with an explanation rather than a
        # segfault the SDK would misreport as an SSH problem.
        caller_python_env_var(): caller_py,
    }

    session_payload = _build_session_payload(cfg, command, env_vars)

    run_config = type(config)(
        ssh_key_path=config.ssh_key_path,
        api_base=config.api_base,
        timeout_seconds=timeout,
        poll_interval_seconds=config.poll_interval_seconds,
        log_poll_interval_seconds=config.log_poll_interval_seconds,
    )

    verbose = os.environ.get("MIMIRY_VERBOSE", "1") != "0"

    def _log(msg: str) -> None:
        if verbose:
            print(f"[mimiry] {msg}", file=sys.stderr, flush=True)

    with MimiryClient(token) as client:
        # Fail fast on an impossible gpu/provider combo before paying for a
        # provisioning round-trip, and resolve a GPU family alias (e.g. "A100") to
        # the concrete catalog name the API requires. Best-effort — a flaky
        # availability endpoint won't block submission. See _availability.py.
        resolved_gpu = preflight_gpu_availability(client, cfg.gpu, cfg.provider, cfg.location)
        session_payload["gpu"]["types"] = resolved_gpu

        session = client.create_session(session_payload)
        session_id = session["id"]
        _log(f"session {session_id} submitted")

        try:
            ran_payload, _ = wait_for_started_or_terminal(
                client, session_id, run_config, on_state_change=lambda s: _log(f"state={s}")
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

            try:
                _log(f"waiting for {RESULT_FILE}")
                wait_for_remote_file(
                    target,
                    RESULT_FILE,
                    max_wait_seconds=timeout,
                    terminal_check=terminal_check,
                )

                _log("fetching result")
                raw = fetch_remote_file(target, RESULT_FILE).decode("utf-8", errors="replace")

                _log("signalling done")
                try:
                    signal_done(target)
                except Exception as e:
                    # Result already in hand — don't fail the call over this.
                    _log(f"warning: signal_done failed ({e}); container will time out on its own")
            finally:
                close_control_channel(target)

            try:
                # Verify the result's HMAC before deserializing.
                verified_b64 = verify_result_envelope(raw, hmac_key)
                return parse_result(verified_b64)
            except ResultIntegrityError as e:
                raise ResultIntegrityError(f"{e} (session {session_id})") from e
            except ResultParseError as e:
                raise ResultParseError(f"{e} (session {session_id})") from e
        except Exception:
            # Pull events for the failure narrative.
            try:
                final = client.get_session(session_id, events_tail=-1)
                _log(f"final session payload: state={final.get('state')} stop_reason={final.get('stop_reason')} error={final.get('error')}")
            except Exception:
                pass
            raise
        finally:
            state = (client.get_session(session_id).get("state") or "").lower()
            if state not in TERMINAL_STATES:
                try:
                    client.terminate_session(session_id)
                except Exception:
                    pass


__all__ = ["function", "Function", "FunctionConfig", "SessionError"]
