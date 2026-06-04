"""SSH-JWT authentication against softlaunch.mimiry.com.

Ports the algorithm from .claude/skills/mimiry-softlaunch/scripts/mimiry-auth.sh.
We shell out to ``ssh-keygen`` for signing — implementing the SSH signature
format in pure Python would mean re-deriving an OpenSSH-compatible format from
the ``cryptography`` library, and ``ssh-keygen`` is universally available on
the systems Mimiry users actually run on.

Tokens last 1 hour (Mimiry default). The Token class refreshes itself when
within 5 minutes of expiry.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from mimiry.exceptions import AuthError

_TOKEN_REFRESH_BUFFER_SECONDS = 300  # refresh if < 5 min left
_DEFAULT_TOKEN_TTL_SECONDS = 3600


@dataclass
class Token:
    """A short-lived JWT issued by Mimiry. Self-refreshes when near expiry."""

    access_token: str
    expires_at: float  # unix seconds
    fingerprint: str
    ssh_key_path: Path
    api_base: str

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def is_near_expiry(self) -> bool:
        return time.time() + _TOKEN_REFRESH_BUFFER_SECONDS >= self.expires_at

    def refresh(self) -> "Token":
        """Re-exchange the SSH signature for a fresh JWT. Mutates self."""
        new = exchange_ssh_for_token(self.ssh_key_path, self.api_base)
        self.access_token = new.access_token
        self.expires_at = new.expires_at
        return self

    def get(self) -> str:
        """Return the bearer string, refreshing if near expiry."""
        if self.is_near_expiry:
            self.refresh()
        return self.access_token


def _normalize_key_path(key_path: str | Path) -> Path:
    """Accept either the private key path or the .pub path; return the private path."""
    p = Path(key_path).expanduser()
    if p.suffix == ".pub":
        p = p.with_suffix("")
    if not p.is_file():
        raise AuthError(f"SSH private key not found: {p}")
    if not p.with_suffix(p.suffix + ".pub").is_file() and not Path(f"{p}.pub").is_file():
        raise AuthError(f"SSH public key not found: {p}.pub")
    return p


def _fingerprint(public_key_path: Path) -> str:
    """Run ``ssh-keygen -lf`` to get the SHA256 fingerprint of a public key."""
    if shutil.which("ssh-keygen") is None:
        raise AuthError("ssh-keygen not found on PATH — required for Mimiry auth")
    try:
        out = subprocess.run(
            ["ssh-keygen", "-lf", str(public_key_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise AuthError(f"ssh-keygen -lf failed: {e.stderr.strip()}") from e
    # Format: "<bits> <fingerprint> <comment> (<type>)"
    parts = out.stdout.strip().split()
    if len(parts) < 2:
        raise AuthError(f"unexpected ssh-keygen output: {out.stdout!r}")
    return parts[1]


def _sign(message: bytes, private_key_path: Path) -> bytes:
    """Sign ``message`` with the SSH key under the ``mimiry-auth`` namespace.

    Returns the raw .sig file contents (OpenSSH SSHSIG format), as Mimiry expects.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        msg_path = Path(tmpdir) / "msg"
        msg_path.write_bytes(message)
        try:
            subprocess.run(
                ["ssh-keygen", "-Y", "sign", "-f", str(private_key_path), "-n", "mimiry-auth",
                 str(msg_path)],
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise AuthError(
                f"ssh-keygen -Y sign failed: {e.stderr.decode(errors='replace').strip()}"
            ) from e
        sig_path = Path(f"{msg_path}.sig")
        if not sig_path.is_file():
            raise AuthError("ssh-keygen did not produce a .sig file")
        return sig_path.read_bytes()


def exchange_ssh_for_token(
    ssh_key_path: str | Path,
    api_base: str = "https://softlaunch.mimiry.com",
) -> Token:
    """Do the SSH-signature → JWT exchange. Returns a Token."""
    api_base = api_base.rstrip("/")
    priv = _normalize_key_path(ssh_key_path)
    pub = Path(f"{priv}.pub")

    fingerprint = _fingerprint(pub)
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)

    message = f"{fingerprint}\n{timestamp}\n{nonce}".encode()
    signature_bytes = _sign(message, priv)
    signature_b64 = base64.b64encode(signature_bytes).decode()

    try:
        resp = httpx.post(
            f"{api_base}/api/v1/auth/token",
            headers={
                "X-SSH-Fingerprint": fingerprint,
                "X-SSH-Signature": signature_b64,
                "X-SSH-Timestamp": timestamp,
                "X-SSH-Nonce": nonce,
                "Content-Type": "application/json",
            },
            json={"expires_in": _DEFAULT_TOKEN_TTL_SECONDS},
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        raise AuthError(f"token exchange request failed: {e}") from e

    if resp.status_code != 200:
        raise AuthError(
            f"token exchange returned {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    access_token = body.get("access_token")
    if not access_token:
        raise AuthError(f"token exchange response missing access_token: {body}")

    expires_in = body.get("expires_in", _DEFAULT_TOKEN_TTL_SECONDS)
    return Token(
        access_token=access_token,
        expires_at=time.time() + float(expires_in),
        fingerprint=fingerprint,
        ssh_key_path=priv,
        api_base=api_base,
    )


def get_token(
    ssh_key_path: str | Path | None = None,
    api_base: str = "https://softlaunch.mimiry.com",
) -> Token:
    """Get a fresh Token. Falls back to ``MIMIRY_SSH_KEY`` env var when ``ssh_key_path`` is None."""
    if ssh_key_path is None:
        env_key = os.environ.get("MIMIRY_SSH_KEY")
        if not env_key:
            raise AuthError(
                "no SSH key provided — pass ssh_key_path=, set MIMIRY_SSH_KEY env var, "
                "or call mimiry.configure(ssh_key_path=...) first"
            )
        ssh_key_path = env_key
    return exchange_ssh_for_token(ssh_key_path, api_base)
