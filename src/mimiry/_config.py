"""Process-global SDK configuration. Holds auth + API base URL.

Users either set MIMIRY_SSH_KEY env var or call ``mimiry.configure(ssh_key_path=...)``
once. Subsequent calls override.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_BASE = "https://softlaunch.mimiry.com"
DEFAULT_TIMEOUT_SECONDS = 1800  # 30 min — must accommodate ~2 min cold start + work


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
    """Return the active configuration. Auto-bootstraps from env vars on first read."""
    if _config.ssh_key_path is None:
        env_key = os.environ.get("MIMIRY_SSH_KEY")
        if env_key:
            _config.ssh_key_path = Path(env_key).expanduser()
    if "MIMIRY_API_BASE" in os.environ:
        _config.api_base = os.environ["MIMIRY_API_BASE"].rstrip("/")
    return _config
