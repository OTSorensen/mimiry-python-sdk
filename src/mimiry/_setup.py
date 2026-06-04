"""Interactive auth wizard for first-time setup — ``mimiry setup`` / ``mimiry init``.

A one-time interactive wizard for Mimiry's SSH-JWT auth (there is no API-key
flow). The wizard:

1. Generates an ed25519 key at ``~/.ssh/mimiry`` if one doesn't already exist.
2. Shows the public key and opens the portal so the user can register it
   (there is no key-registration API — registration is manual in the portal).
3. Writes ``MIMIRY_SSH_KEY`` to the user's shell rc file (with confirmation).
4. Runs a balance check to verify the whole chain end-to-end.

Everything here is deliberately dependency-free (stdlib only) and degrades
gracefully when run non-interactively (EOF on a prompt falls back to the
shown default).
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from mimiry._auth import get_token
from mimiry._client import MimiryClient
from mimiry._config import DEFAULT_API_BASE

DEFAULT_KEY_PATH = Path("~/.ssh/mimiry").expanduser()

# Marker so we can recognise (and avoid duplicating) a line we previously wrote.
_RC_MARKER = "# added by `mimiry setup`"


# ────────────────────────── small I/O helpers ──────────────────────────


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt(message: str, default: str = "") -> str:
    """Read a line from the user, returning ``default`` on empty input or EOF."""
    try:
        answer = input(message).strip()
    except EOFError:
        return default
    return answer or default


def _confirm(message: str, default: bool = True) -> bool:
    """Yes/no prompt. EOF or empty input returns ``default``."""
    suffix = " [Y/n] " if default else " [y/N] "
    answer = _prompt(message + suffix).lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _step(n: int, title: str) -> None:
    print(f"\n\033[1m[{n}/4] {title}\033[0m" if sys.stdout.isatty() else f"\n[{n}/4] {title}")


# ────────────────────────── step 1: key generation ──────────────────────────


def _ensure_key(key_path: Path) -> Path:
    """Return the private-key path, generating an ed25519 keypair if missing."""
    priv = key_path.expanduser()
    pub = Path(f"{priv}.pub")

    if priv.is_file():
        print(f"  Using existing key: {priv}")
        if not pub.is_file():
            # Private key without its .pub — regenerate the public half from it.
            print(f"  Public key {pub} missing — deriving it from the private key.")
            with pub.open("w") as fh:
                subprocess.run(["ssh-keygen", "-y", "-f", str(priv)], stdout=fh, check=True)
        return priv

    priv.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    print(f"  No key at {priv} — generating a new ed25519 keypair...")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-C", "mimiry"],
        check=True,
        capture_output=True,
    )
    print(f"  Created {priv} and {pub}")
    return priv


# ────────────────────────── step 2: register in portal ──────────────────────────


def _register_key(pub_path: Path, api_base: str) -> None:
    """Show the public key, open the portal, and wait for the user to register it."""
    pub_text = pub_path.read_text().strip()
    portal_url = api_base.rstrip("/")

    print("\n  Add this PUBLIC key to your Mimiry account:\n")
    print(f"    {pub_text}\n")
    print(f"  In the portal: {portal_url} → Profile → SSH Keys → Add Key")
    print("  (Mimiry has no key-registration API yet — this step is manual.)")

    if _is_interactive():
        if _confirm("\n  Open the portal in your browser now?", default=True):
            try:
                webbrowser.open(portal_url)
            except Exception:
                pass  # headless / no browser — the URL is printed above anyway
        _prompt("\n  Press Enter once the key is registered to continue...")


# ────────────────────────── step 3: write shell rc ──────────────────────────


def _detect_rc() -> tuple[Path, str]:
    """Return ``(rc_path, export_line)`` appropriate for the user's shell."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    if shell.endswith("zsh"):
        return home / ".zshrc", "export MIMIRY_SSH_KEY={path}"
    if shell.endswith("fish"):
        return home / ".config/fish/config.fish", "set -x MIMIRY_SSH_KEY {path}"
    # bash and everything else
    return home / ".bashrc", "export MIMIRY_SSH_KEY={path}"


def _write_rc(key_path: Path) -> None:
    """Persist ``MIMIRY_SSH_KEY`` to the shell rc file, after confirming with the user."""
    rc_path, template = _detect_rc()
    export_line = template.format(path=key_path)

    existing = rc_path.read_text() if rc_path.is_file() else ""
    if export_line in existing:
        print(f"  {rc_path} already exports MIMIRY_SSH_KEY={key_path} — nothing to do.")
        return
    if "MIMIRY_SSH_KEY" in existing:
        print(f"  Note: {rc_path} already sets MIMIRY_SSH_KEY to a different value.")
        if not _confirm(f"  Append the new value ({key_path}) anyway?", default=False):
            print("  Skipped rc update — set MIMIRY_SSH_KEY yourself if needed.")
            return

    print(f"\n  I'd like to add this line to {rc_path}:\n\n    {export_line}\n")
    if not _confirm("  Write it?", default=True):
        print(f"  Skipped. To set it manually:\n    {export_line}")
        return

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    needs_nl = existing and not existing.endswith("\n")
    with rc_path.open("a") as fh:
        fh.write(("\n" if needs_nl else "") + f"\n{_RC_MARKER}\n{export_line}\n")
    print(f"  Wrote MIMIRY_SSH_KEY to {rc_path}.")
    print(f"  Restart your shell or run:  source {rc_path}")


# ────────────────────────── step 4: verify ──────────────────────────


def _verify(key_path: Path, api_base: str) -> bool:
    """Run a balance check through the real auth path. Returns True on success."""
    try:
        token = get_token(str(key_path), api_base)
        with MimiryClient(token) as client:
            balance = client.get_balance()
    except Exception as e:  # noqa: BLE001 — surface any failure as actionable guidance
        print(f"  Balance check failed: {e}")
        print("  This usually means the public key isn't registered yet.")
        print(f"  Register {key_path}.pub in the portal, then re-run `mimiry setup`.")
        return False

    bal = balance.get("balance")
    cur = balance.get("currency", "EUR")
    print(f"  Authenticated. Balance: {bal} {cur}")
    return True


# ────────────────────────── orchestration ──────────────────────────


def run_setup_wizard(
    ssh_key_path: str | Path | None = None,
    api_base: str | None = None,
) -> int:
    """Run the interactive setup wizard. Returns a process exit code."""
    api_base = (api_base or os.environ.get("MIMIRY_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    key_path = Path(ssh_key_path).expanduser() if ssh_key_path else DEFAULT_KEY_PATH

    print("Welcome to Mimiry. This wizard sets up SSH-key auth for the SDK.")

    _step(1, "SSH key")
    priv = _ensure_key(key_path)
    pub = Path(f"{priv}.pub")

    _step(2, "Register your key in the portal")
    _register_key(pub, api_base)

    _step(3, "Persist MIMIRY_SSH_KEY")
    # Make the key visible to this process so the verification step works even
    # before the user reloads their shell.
    os.environ["MIMIRY_SSH_KEY"] = str(priv)
    _write_rc(priv)

    _step(4, "Verify")
    ok = _verify(priv, api_base)

    if ok:
        print("\n\033[32m✓ All set.\033[0m You're ready to run GPU jobs with Mimiry.")
        print("  Try:  mimiry availability --gpu-family T4")
        return 0
    print("\nSetup incomplete — finish registering your key, then re-run `mimiry setup`.")
    return 1
