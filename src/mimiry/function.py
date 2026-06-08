"""``@mimiry.function`` decorator and the ``Function`` runtime class.

v1 contract: every ``.remote()`` call creates a fresh Mimiry session, runs the
user's function inside it, and SSHes in to fetch the serialized return value.
Cold-start is ~2 minutes (measured 2026-05-19). There is no warm pool in v1
— see PROGRESS.md Step 7 for the v2/backend plan.

Why SSH and not the /logs endpoint: see ``_ssh.py`` docstring. tl;dr the
softlaunch logs endpoint currently doesn't stream stdout in any usable
window. v2 with a proper result store will retire the SSH path.
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
    pack_call,
    parse_result,
    payload_env_var,
)
from mimiry._session import (
    raise_if_failed,
    wait_for_ssh_ready,
    wait_for_started_or_terminal,
)
from mimiry._ssh import (
    RESULT_FILE,
    close_control_channel,
    fetch_remote_file,
    open_control_channel,
    signal_done,
    ssh_target_from_session,
    wait_for_remote_file,
    wait_for_sshd,
)
from mimiry.exceptions import ResultParseError, SessionError
from mimiry.image import Image, normalize_image


def _public_key(ssh_key_path: Path) -> str:
    pub = Path(f"{ssh_key_path}.pub")
    return pub.read_text().strip()


def _session_name(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"


@dataclass
class FunctionConfig:
    gpu: str = "T4"
    gpu_count: int = 1
    # Ubuntu 24.04 ships Python 3.12 by default — matches recent local SDK runtimes.
    # cloudpickle code objects don't roundtrip across major.minor mismatches, so
    # the container's Python must match the caller's. See README "Python version
    # compatibility".
    image: Image | str = "nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04"
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
    gpu: str = "T4",
    gpu_count: int = 1,
    # Ubuntu 24.04 ships Python 3.12 by default — matches recent local SDK runtimes.
    # cloudpickle code objects don't roundtrip across major.minor mismatches, so
    # the container's Python must match the caller's. See README "Python version
    # compatibility".
    image: Image | str = "nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04",
    timeout: int | None = None,
    provider: str | None = None,
    location: str | None = None,
    env: dict[str, str] | None = None,
    name: str | None = None,
) -> Callable[[Callable], Function]:
    """Decorator: turn a Python function into a Mimiry-remote callable.

    Example::

        @mimiry.function(gpu="T4", provider="gcp", image="nvcr.io/nvidia/pytorch:24.01-py3")
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
    payload_b64 = pack_call(fn, args, kwargs)
    command = build_bootstrap_script(image_install_prefix=image.install_prefix())
    env_vars = {payload_env_var(): payload_b64}

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
        # provisioning round-trip. Best-effort — a flaky availability endpoint
        # won't block submission. See _availability.py.
        preflight_gpu_availability(client, cfg.gpu, cfg.provider, cfg.location)

        session = client.create_session(session_payload)
        session_id = session["id"]
        _log(f"session {session_id} submitted")

        try:
            ran_payload, _ = wait_for_started_or_terminal(
                client, session_id, run_config, on_state_change=lambda s: _log(f"state={s}")
            )
            raise_if_failed(ran_payload, client=client)

            _log("waiting for ssh.host to be populated")
            ssh_ready = wait_for_ssh_ready(client, session_id, run_config)
            raise_if_failed(ssh_ready, client=client)

            target = ssh_target_from_session(ssh_ready, config.ssh_key_path)
            _log(f"sshing into {target.host}:{target.port}")
            wait_for_sshd(target)

            _log("opening SSH control channel (multiplexing for the bootstrap install storm)")
            target = open_control_channel(target)

            _terminal_states = {"terminated", "completed", "failed", "stopped", "provision_failed"}

            def _terminal_check() -> str | None:
                try:
                    s = (client.get_session(session_id).get("state") or "").lower()
                except Exception:
                    return None
                return s if s in _terminal_states else None

            try:
                _log(f"waiting for {RESULT_FILE}")
                wait_for_remote_file(
                    target,
                    RESULT_FILE,
                    max_wait_seconds=timeout,
                    terminal_check=_terminal_check,
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
                return parse_result(raw)
            except ResultParseError as e:
                raise ResultParseError(f"{e} (session {session_id})") from e
        except Exception as e:
            # Pull events for the failure narrative.
            try:
                final = client.get_session(session_id, events_tail=-1)
                _log(f"final session payload: state={final.get('state')} stop_reason={final.get('stop_reason')} error={final.get('error')}")
            except Exception:
                pass
            raise
        finally:
            state = (client.get_session(session_id).get("state") or "").lower()
            if state not in {"terminated", "completed", "failed", "stopped", "provision_failed"}:
                try:
                    client.terminate_session(session_id)
                except Exception:
                    pass


__all__ = ["function", "Function", "FunctionConfig", "SessionError"]
