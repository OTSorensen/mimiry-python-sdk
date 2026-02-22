"""Mimiry Python SDK - Programmatic access to GPU cloud resources."""

__version__ = "0.1.0"

from .client import MimiryClient
from .exceptions import (
    MimiryError,
    AuthenticationError,
    InsufficientCreditsError,
    InsufficientScopeError,
    NotFoundError,
    RateLimitError,
    ServerError,
)

__all__ = [
    "MimiryClient",
    "MimiryError",
    "AuthenticationError",
    "InsufficientCreditsError",
    "InsufficientScopeError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
]
