"""mimiry — Python SDK for Mimiry GPU compute (softlaunch).

v0.2.0 — wraps the existing /api/compute/v1/sessions API. See README.md.
"""

from mimiry._config import configure, get_config
from mimiry.exceptions import (
    MimiryError,
    AuthError,
    SessionError,
    SessionFailed,
    SessionTimeout,
    ResultParseError,
)
from mimiry.function import function, Function
from mimiry.image import Image
from mimiry.run import run

__version__ = "0.3.0"

__all__ = [
    "__version__",
    "configure",
    "get_config",
    "function",
    "Function",
    "Image",
    "run",
    "MimiryError",
    "AuthError",
    "SessionError",
    "SessionFailed",
    "SessionTimeout",
    "ResultParseError",
]
