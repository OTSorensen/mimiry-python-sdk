"""SSH-based result retrieval for Mimiry sessions.

# Why SSH

The softlaunch ``/sessions/{id}/logs`` endpoint does not return container
stdout — it returns the SSH proxy's login-session banner only (re-confirmed
on 2026-05-31 with a known-good probe). Container stdout is not surfaced by
the public API today. Until a real result-storage path exists, the SDK
retrieves function return values out-of-band: the container writes the
serialized return value to a known path, blocks on a "done" flag, and the
SDK SSHes in to pull the file and release the container.

This module is intentionally thin — it shells out to the system ``ssh``
binary rather than depending on Paramiko or asyncssh. Every softlaunch user
already has OpenSSH installed (it's a prereq for the existing CLI flow),
and shelling out keeps the trust surface small.

# Connection multiplexing

The container's bootstrap installs Python+pip+cloudpickle on the fly, which
saturates the single-vCPU instance for ~60–120 s and starves new SSH
handshakes (observed empirically: a fresh ``ssh`` invocation can stall past
a 15 s timeout while the install runs). We work around this by opening one
ControlMaster connection up front and reusing it via ``ControlPath`` for
every subsequent command. Pre-fork handshakes happen once; subsequent
commands ride the existing channel without re-negotiating.

The trade-off is that we route every result through ``mimiry-ssh-proxy``.
v2's dedicated result store will replace this entirely.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mimiry.exceptions import MimiryError

# Paths the container and SDK agree on.
RESULT_FILE = "/tmp/mimiry_result.b64"
DONE_FLAG = "/tmp/mimiry_done"
ERROR_FILE = "/tmp/mimiry_bootstrap_error"

# How long the container will wait for the SDK to fetch its result before giving up.
# Bounds the runaway-cost scenario if the SDK crashes after submitting.
CONTAINER_HOLD_TIMEOUT_SECONDS = 1800  # 30 min, ~€0.18 worst case at T4/GCP rates

# SSH command defaults.
_CONNECT_TIMEOUT_SECONDS = 30
_DEFAULT_CMD_TIMEOUT_SECONDS = 60
_CMD_RETRIES = 3
_CMD_RETRY_BACKOFF_SECONDS = 3.0


class SSHError(MimiryError):
    """SSH transport failure (connect refused, command nonzero, etc.)."""


@dataclass
class SshTarget:
    host: str
    port: int
    username: str
    key_path: Path
    control_socket: Path | None = field(default=None)

    def with_control_socket(self) -> "SshTarget":
        """Return a copy with a fresh ControlMaster socket path assigned."""
        sock = Path(tempfile.gettempdir()) / f"mimiry-ssh-{uuid.uuid4().hex[:12]}"
        return SshTarget(
            host=self.host,
            port=self.port,
            username=self.username,
            key_path=self.key_path,
            control_socket=sock,
        )


def ssh_target_from_session(payload: dict, key_path: Path) -> SshTarget:
    """Pull the SSH coordinates out of a session detail response."""
    ssh = payload.get("ssh") or {}
    host = ssh.get("host")
    port = ssh.get("port")
    user = ssh.get("username") or "root"
    if not host or not port:
        raise SSHError(
            f"session payload missing ssh.host/port — was ssh_enabled set? payload keys={list(payload)}"
        )
    return SshTarget(host=host, port=int(port), username=user, key_path=key_path)


def _common_ssh_opts(target: SshTarget) -> list[str]:
    """SSH client options shared by every command. ControlMaster multiplexing
    keeps subsequent commands cheap during the bootstrap install storm.
    """
    opts = [
        "-i", str(target.key_path),
        "-p", str(target.port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", f"ConnectTimeout={_CONNECT_TIMEOUT_SECONDS}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
    ]
    if target.control_socket is not None:
        opts += [
            "-o", f"ControlPath={target.control_socket}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=10m",
        ]
    return opts


def _ssh_cmd(
    target: SshTarget,
    remote_cmd: str,
    *,
    timeout: int = _DEFAULT_CMD_TIMEOUT_SECONDS,
    retries: int = _CMD_RETRIES,
) -> subprocess.CompletedProcess:
    """Run a remote command. Retries on TimeoutExpired or transient nonzero exits.

    We only retry transport-level failures (timeouts, exit 255 = ssh client
    failure). Nonzero exits from the *remote* command (e.g. ``test -f``
    returning 1) are surfaced as-is — those are signals, not bugs.
    """
    if shutil.which("ssh") is None:
        raise SSHError("'ssh' binary not found on PATH")

    args = ["ssh", *_common_ssh_opts(target), f"{target.username}@{target.host}", remote_cmd]

    last_err: str | None = None
    for attempt in range(1, retries + 1):
        try:
            return subprocess.run(args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            last_err = f"timeout after {timeout}s"
            if attempt >= retries:
                raise SSHError(
                    f"ssh '{remote_cmd[:60]}' timed out after {retries} attempts: {last_err}"
                ) from e
            time.sleep(_CMD_RETRY_BACKOFF_SECONDS)
            continue

    raise SSHError(f"ssh '{remote_cmd[:60]}' failed: {last_err}")


def open_control_channel(target: SshTarget, *, max_wait_seconds: int = 300) -> SshTarget:
    """Open a ControlMaster connection up front.

    Returns a target with the control socket attached. All subsequent
    :func:`_ssh_cmd` calls using that target will multiplex through the
    persistent connection, avoiding a fresh TCP+TLS+SSH handshake per call.

    Blocks until the master is established (or ``max_wait_seconds`` elapses).
    """
    multiplexed = target.with_control_socket()
    if shutil.which("ssh") is None:
        raise SSHError("'ssh' binary not found on PATH")

    deadline = time.monotonic() + max_wait_seconds
    last_err: str | None = None
    while time.monotonic() < deadline:
        # ``-N`` = no remote command, ``-f`` = background once authenticated.
        master_args = [
            "ssh",
            *_common_ssh_opts(multiplexed),
            "-N", "-f",
            f"{multiplexed.username}@{multiplexed.host}",
        ]
        try:
            r = subprocess.run(master_args, capture_output=True, timeout=_CONNECT_TIMEOUT_SECONDS + 5)
        except subprocess.TimeoutExpired:
            last_err = "master handshake timed out"
            time.sleep(3)
            continue

        if r.returncode == 0:
            # Confirm the channel works with a no-op command.
            probe = _ssh_cmd(multiplexed, "true", timeout=15, retries=2)
            if probe.returncode == 0:
                return multiplexed
            last_err = f"control channel probe failed: {probe.stderr.decode(errors='replace')[:200]}"

        else:
            last_err = (r.stderr or r.stdout).decode("utf-8", errors="replace").strip()[:300]

        time.sleep(3)

    raise SSHError(
        f"could not open SSH control channel to {target.host}:{target.port} within "
        f"{max_wait_seconds}s — last: {last_err}"
    )


def close_control_channel(target: SshTarget) -> None:
    """Tear down the ControlMaster connection. Idempotent and best-effort."""
    if target.control_socket is None:
        return
    try:
        subprocess.run(
            [
                "ssh",
                *_common_ssh_opts(target),
                "-O", "exit",
                f"{target.username}@{target.host}",
            ],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    try:
        target.control_socket.unlink(missing_ok=True)
    except OSError:
        pass


def wait_for_sshd(target: SshTarget, *, max_wait_seconds: int = 300) -> None:
    """Block until the container's sshd accepts a connection. Mimiry's ssh-proxy
    seems to accept connections almost immediately after ``state=started``, but
    we still need a quick probe to avoid racing the very first connect.
    """
    deadline = time.monotonic() + max_wait_seconds
    last_err: str | None = None
    while time.monotonic() < deadline:
        try:
            r = subprocess.run(
                ["ssh", *_common_ssh_opts(target), f"{target.username}@{target.host}", "echo ok"],
                capture_output=True,
                timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip() == b"ok":
                return
            last_err = (r.stderr or r.stdout).decode("utf-8", errors="replace").strip()
        except subprocess.TimeoutExpired:
            last_err = "ssh connect timed out"
        time.sleep(3)
    raise SSHError(
        f"sshd not reachable on {target.host}:{target.port} after {max_wait_seconds}s — last: {last_err}"
    )


def wait_for_remote_file(
    target: SshTarget,
    path: str,
    *,
    max_wait_seconds: int,
    poll_interval: float = 5.0,
    terminal_check: Callable[[], str | None] | None = None,
) -> None:
    """Poll for a file to exist on the remote container.

    During bootstrap the container is CPU-bound on apt-get/pip. Polling too
    aggressively just compounds the load — 5 s is a sweet spot in practice.

    ``terminal_check``, if provided, is called every poll to fetch the
    session's current state. If it returns a terminal state string before
    the file appears, we raise immediately with a clear "container exited
    early" message — better than sitting in the SSH retry loop for 30 minutes
    after the container has already gone away.
    """
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        r = _ssh_cmd(
            target,
            f"test -f {path} && echo present || echo missing",
            timeout=_DEFAULT_CMD_TIMEOUT_SECONDS,
        )
        if r.returncode == 0 and r.stdout.strip() == b"present":
            return
        # Surface bootstrap errors early if the container reported one.
        e = _ssh_cmd(
            target,
            f"test -f {ERROR_FILE} && cat {ERROR_FILE} || true",
            timeout=_DEFAULT_CMD_TIMEOUT_SECONDS,
        )
        if e.returncode == 0 and e.stdout.strip():
            raise SSHError(
                f"container bootstrap reported error:\n{e.stdout.decode(errors='replace')}"
            )
        if terminal_check is not None:
            terminal_state = terminal_check()
            if terminal_state is not None:
                raise SSHError(
                    f"session reached terminal state '{terminal_state}' before {path} "
                    "appeared — the container exited before our bootstrap could write a result. "
                    "Common cause: the bootstrap apt-get/pip install hit a non-zero exit "
                    "(running under 'set -e'). Try a base image that ships python3+pip "
                    "preinstalled (e.g. nvcr.io/nvidia/pytorch:24.01-py3) to skip the install step."
                )
        time.sleep(poll_interval)
    raise SSHError(f"file {path} did not appear on remote within {max_wait_seconds}s")


def fetch_remote_file(target: SshTarget, path: str) -> bytes:
    """Cat a remote file and return its bytes."""
    r = _ssh_cmd(target, f"cat {path}", timeout=120)
    if r.returncode != 0:
        raise SSHError(
            f"ssh cat {path} returned {r.returncode}: {r.stderr.decode(errors='replace').strip()}"
        )
    return r.stdout


def signal_done(target: SshTarget) -> None:
    """Touch the done flag so the container's wait-loop can exit."""
    r = _ssh_cmd(target, f"touch {DONE_FLAG}", timeout=30)
    if r.returncode != 0:
        raise SSHError(
            f"ssh touch {DONE_FLAG} returned {r.returncode}: "
            f"{r.stderr.decode(errors='replace').strip()}"
        )
