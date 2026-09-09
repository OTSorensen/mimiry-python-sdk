"""mimiry — Python SDK for Mimiry GPU compute.

Wraps the /api/compute/v1/sessions API. See README.md.
"""

from mimiry._config import configure, get_config
from mimiry.exceptions import (
    MimiryError,
    AuthError,
    SessionError,
    SessionFailed,
    SessionTimeout,
    MapError,
    ResultParseError,
    ResultIntegrityError,
)
from mimiry._serialization import RemoteFunctionError
from mimiry.function import function, Function
from mimiry.image import Image
from mimiry.run import run

__version__ = "0.3.3"

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
    "MapError",
    "ResultParseError",
    "ResultIntegrityError",
    "RemoteFunctionError",
]
