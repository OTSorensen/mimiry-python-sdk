"""``mimiry.run()`` — run a raw bash command on a Mimiry GPU and return its stdout/stderr.

This is the escape hatch for users who don't want a Python function decorator:
ffmpeg jobs, ``nvidia-smi`` probes, shell-only ML pipelines, etc.

Same SSH-based retrieval as ``@mimiry.function`` — the container captures
output to a file, blocks on a done flag, and the SDK pulls the file when
ready. See ``_ssh.py`` for the rationale.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mimiry._auth import get_token
from mimiry._client import MimiryClient
from mimiry._config import Config, get_config
from mimiry._session import raise_if_failed, wait_for_ssh_ready, wait_for_started_or_terminal
from mimiry._ssh import (
    CONTAINER_HOLD_TIMEOUT_SECONDS,
    DONE_FLAG,
    close_control_channel,
    fetch_remote_file,
    open_control_channel,
    signal_done,
    ssh_target_from_session,
    wait_for_remote_file,
    wait_for_sshd,
)
from mimiry.image import Image, normalize_image

# Paths the container writes; mimics the function flow but with plaintext output rather
# than a serialized payload.
_RUN_OUTPUT_FILE = "/tmp/mimiry_run_output.log"
_RUN_EXIT_FILE = "/tmp/mimiry_run_exit"


@dataclass
class RunResult:
    session_id: str
    state: str
    stop_reason: str | None
    logs: str  # combined stdout + stderr from the user command
    exit_code: int | None


def _session_name() -> str:
    return f"mimiry-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _quote(s: str) -> str:
    if "'" not in s:
        return f"'{s}'"
    return "'" + s.replace("'", "'\\''") + "'"


def _wrap_command(user_command: str, install_prefix: str) -> str:
    """Wrap the user's command so output and exit code are captured to files
    the SDK can fetch over SSH, then block on the done flag.
    """
    pre = (install_prefix + " && ") if install_prefix else ""
    inner = (
        f"set -o pipefail; ( {pre}({user_command}) ) "
        f"> {_RUN_OUTPUT_FILE} 2>&1; "
        f"echo $? > {_RUN_EXIT_FILE}; "
        # Block until SDK signals done, or hard timeout.
        f"for _i in $(seq 1 {CONTAINER_HOLD_TIMEOUT_SECONDS}); do "
        f"  [ -f {DONE_FLAG} ] && break; "
        f"  sleep 1; "
        f"done"
    )
    return f"bash -c {_quote(inner)}"


def run(
    image: Image | str,
    *,
    gpu: str = "T4",
    gpu_count: int = 1,
    command: str,
    timeout: int | None = None,
    provider: str | None = None,
    location: str | None = None,
    environment_vars: dict[str, str] | None = None,
) -> RunResult:
    """Execute ``command`` on a fresh Mimiry GPU session and return its output.

    Example::

        result = mimiry.run(
            image="nvcr.io/nvidia/cuda:12.6.0-runtime-ubuntu22.04",
            gpu="T4",
            provider="gcp",
            command="nvidia-smi",
        )
        print(result.logs)
    """
    config = get_config()
    if config.ssh_key_path is None:
        raise RuntimeError(
            "Mimiry SDK is not configured. Set MIMIRY_SSH_KEY or call mimiry.configure(...)."
        )

    img = normalize_image(image)
    wrapped = _wrap_command(command, img.install_prefix())

    timeout_s = timeout or config.timeout_seconds
    run_config = Config(
        ssh_key_path=config.ssh_key_path,
        api_base=config.api_base,
        timeout_seconds=timeout_s,
        poll_interval_seconds=config.poll_interval_seconds,
        log_poll_interval_seconds=config.log_poll_interval_seconds,
    )

    token = get_token(config.ssh_key_path, config.api_base)
    pub_key = Path(f"{config.ssh_key_path}.pub").read_text().strip()

    gpu_spec: dict = {"types": [gpu], "count": gpu_count}
    if provider is not None:
        gpu_spec["provider"] = provider
    if location is not None:
        gpu_spec["location"] = location

    merged_env = {**img.env_vars, **(environment_vars or {})}

    payload = {
        "name": _session_name(),
        "image": {"uri": img.uri},
        "gpu": gpu_spec,
        "command": wrapped,
        "environment_vars": merged_env,
        "ssh_enabled": True,
        "ssh_public_key": pub_key,
        "auto_terminate": {"mode": "on_complete"},
    }

    verbose = os.environ.get("MIMIRY_VERBOSE", "1") != "0"

    def _log(msg: str) -> None:
        if verbose:
            print(f"[mimiry] {msg}", file=sys.stderr, flush=True)

    with MimiryClient(token) as client:
        session = client.create_session(payload)
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

            _log("opening SSH control channel")
            target = open_control_channel(target)

            _terminal_states = {"terminated", "completed", "failed", "stopped", "provision_failed"}

            def _terminal_check() -> str | None:
                try:
                    s = (client.get_session(session_id).get("state") or "").lower()
                except Exception:
                    return None
                return s if s in _terminal_states else None

            try:
                _log(f"waiting for {_RUN_EXIT_FILE}")
                wait_for_remote_file(
                    target,
                    _RUN_EXIT_FILE,
                    max_wait_seconds=timeout_s,
                    terminal_check=_terminal_check,
                )

                _log("fetching output")
                logs = fetch_remote_file(target, _RUN_OUTPUT_FILE).decode("utf-8", errors="replace")
                exit_raw = fetch_remote_file(target, _RUN_EXIT_FILE).decode("utf-8", errors="replace").strip()
                try:
                    exit_code = int(exit_raw)
                except ValueError:
                    exit_code = None

                try:
                    signal_done(target)
                except Exception as e:
                    _log(f"warning: signal_done failed ({e}); container will time out on its own")
            finally:
                close_control_channel(target)

            final_payload = client.get_session(session_id)
            return RunResult(
                session_id=session_id,
                state=final_payload.get("state", "?"),
                stop_reason=final_payload.get("stop_reason"),
                logs=logs,
                exit_code=exit_code,
            )
        finally:
            state = (client.get_session(session_id).get("state") or "").lower()
            if state not in {"terminated", "completed", "failed", "stopped", "provision_failed"}:
                try:
                    client.terminate_session(session_id)
                except Exception:
                    pass
