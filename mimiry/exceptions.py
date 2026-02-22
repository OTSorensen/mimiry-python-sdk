"""Custom exception classes for the Mimiry SDK."""


class MimiryError(Exception):
    """Base exception for all Mimiry SDK errors."""

    def __init__(self, message: str, status_code: int = None, response_body: dict = None):
        self.message = message
        self.status_code = status_code
        self.response_body = response_body or {}
        super().__init__(self.message)

    def __str__(self):
        if self.status_code:
            return f"[{self.status_code}] {self.message}"
        return self.message


class AuthenticationError(MimiryError):
    """Raised when the API key is invalid or missing (HTTP 401)."""
    pass


class InsufficientCreditsError(MimiryError):
    """Raised when the user does not have enough credits (HTTP 402)."""
    pass


class InsufficientScopeError(MimiryError):
    """Raised when the API key lacks required scopes (HTTP 403)."""
    pass


class NotFoundError(MimiryError):
    """Raised when the requested resource is not found (HTTP 404)."""
    pass


class RateLimitError(MimiryError):
    """Raised when rate limits are exceeded (HTTP 429)."""
    pass


class ServerError(MimiryError):
    """Raised when the server returns an internal error (HTTP 5xx)."""
    pass


# Map HTTP status codes to exception classes
STATUS_CODE_MAP = {
    401: AuthenticationError,
    402: InsufficientCreditsError,
    403: InsufficientScopeError,
    404: NotFoundError,
    429: RateLimitError,
}


def raise_for_status(status_code: int, response_body: dict):
    """Raise an appropriate exception based on the HTTP status code."""
    if 200 <= status_code < 300:
        return

    message = response_body.get("error", response_body.get("message", "Unknown error"))

    exc_class = STATUS_CODE_MAP.get(status_code)
    if exc_class:
        raise exc_class(message, status_code=status_code, response_body=response_body)

    if status_code >= 500:
        raise ServerError(message, status_code=status_code, response_body=response_body)

    raise MimiryError(message, status_code=status_code, response_body=response_body)
