"""Process-global SDK configuration. Holds auth + API base URL.

Key-path resolution precedence (highest first):

1. an explicit ``mimiry.configure(ssh_key_path=...)`` call or the ``--ssh-key``
   CLI flag
2. the ``MIMIRY_SSH_KEY`` environment variable
3. the on-disk config file written by ``mimiry setup``
   (``$XDG_CONFIG_HOME/mimiry/config.toml``, default ``~/.config/mimiry/config.toml``)

The config file stores **only the path** to the SSH key — never the key
material itself, and never a token. That keeps the file low-sensitivity: the
real secret stays in ``~/.ssh/mimiry`` behind its own ``0600`` perms. We still
write the config ``0600`` and refuse to read it if it's group/other-writable,
since a writable config would be a key-redirection injection vector.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_BASE = "https://softlaunch.mimiry.com"
DEFAULT_TIMEOUT_SECONDS = 1800  # 30 min — must accommodate ~2 min cold start + work

# Group/other WRITABLE bits. We reject a config carrying these on read: an
# attacker who can write the file could repoint the SDK at a key they control.
# Readability is *not* checked — the file holds only a path, which isn't secret.
_INSECURE_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH  # 0o022


@dataclass
class Config:
    ssh_key_path: Path | None = None
    api_base: str = DEFAULT_API_BASE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    poll_interval_seconds: float = 5.0
    # Tighter than state polling — the marker-emission → auto_terminate window is
    # only the wrapper's tail sleep (~20s), so we need ~2s cadence to catch it reliably.
    log_poll_interval_seconds: float = 2.0
    extras: dict = field(default_factory=dict)


_config = Config()


# ────────────────────────── on-disk config file ──────────────────────────


def config_path() -> Path:
    """Path to the persisted config file (honours ``$XDG_CONFIG_HOME``)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "mimiry" / "config.toml"


def save_key_path(ssh_key_path: str | Path, path: Path | None = None) -> Path:
    """Persist the SSH key *path* (never key material) to a ``0600`` config file.

    The write is atomic — content goes to a temp file in the same directory and
    is ``os.replace``-d into place — so a crash mid-write can't leave a
    truncated config behind. The containing directory is created ``0700``.
    """
    cfg_path = path or config_path()
    cfg_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    key_str = str(Path(ssh_key_path).expanduser())
    # TOML-escape backslashes/quotes (defensive — WSL paths are POSIX, but a
    # Windows-style path could carry backslashes).
    escaped = key_str.replace("\\", "\\\\").replace('"', '\\"')
    body = (
        "# Mimiry SDK config — written by `mimiry setup`.\n"
        "# Holds only the path to your SSH key, never the key itself.\n"
        f'ssh_key_path = "{escaped}"\n'
    )

    fd, tmp_name = tempfile.mkstemp(dir=str(cfg_path.parent), prefix=".config-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, cfg_path)  # atomic on POSIX
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return cfg_path


def read_key_path(path: Path | None = None) -> Path | None:
    """Return the key path stored in the config file, or ``None`` if absent.

    Raises ``PermissionError`` if the file is group/other-writable, since a
    writable config is a key-redirection injection vector.
    """
    cfg_path = path or config_path()
    try:
        st = cfg_path.stat()
    except FileNotFoundError:
        return None
    if st.st_mode & _INSECURE_WRITE_BITS:
        raise PermissionError(
            f"{cfg_path} is writable by group/other ({oct(stat.S_IMODE(st.st_mode))}); "
            f"refusing to trust it. Fix with:  chmod 600 {cfg_path}"
        )
    value = _load_toml(cfg_path.read_text()).get("ssh_key_path")
    return Path(value).expanduser() if value else None


def _load_toml(text: str) -> dict:
    """Parse the config file, preferring stdlib ``tomllib`` (Python 3.11+)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 — no stdlib TOML parser
        return _parse_simple_toml(text)
    return tomllib.loads(text)


def _parse_simple_toml(text: str) -> dict:
    """Minimal fallback for Python 3.10: top-level ``key = "value"`` scalars only.

    Sufficient for our schema (a single string key). Not a general TOML parser.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        result[key.strip()] = value
    return result


# ────────────────────────── public configuration API ──────────────────────────


def configure(
    ssh_key_path: str | Path | None = None,
    api_base: str | None = None,
    timeout_seconds: int | None = None,
    poll_interval_seconds: float | None = None,
) -> Config:
    """Set SDK-wide configuration. Any argument left as ``None`` is unchanged."""
    if ssh_key_path is not None:
        _config.ssh_key_path = Path(ssh_key_path).expanduser()
    if api_base is not None:
        _config.api_base = api_base.rstrip("/")
    if timeout_seconds is not None:
        _config.timeout_seconds = timeout_seconds
    if poll_interval_seconds is not None:
        _config.poll_interval_seconds = poll_interval_seconds
    return _config


def get_config() -> Config:
    """Return the active configuration, resolving the key path on first read.

    Precedence: an explicit ``configure()`` call (already on ``_config``) wins;
    otherwise ``MIMIRY_SSH_KEY``; otherwise the on-disk config file.
    """
    if _config.ssh_key_path is None:
        env_key = os.environ.get("MIMIRY_SSH_KEY")
        if env_key:
            _config.ssh_key_path = Path(env_key).expanduser()
        else:
            _config.ssh_key_path = read_key_path()  # may stay None
    if "MIMIRY_API_BASE" in os.environ:
        _config.api_base = os.environ["MIMIRY_API_BASE"].rstrip("/")
    return _config
