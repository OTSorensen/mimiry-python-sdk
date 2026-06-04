"""SDK exception hierarchy. All errors derive from MimiryError so users can catch broadly."""


class MimiryError(Exception):
    """Base class for all SDK errors."""


class AuthError(MimiryError):
    """SSH key signing failed, token exchange rejected, or token expired."""


class SessionError(MimiryError):
    """Generic session-API failure (HTTP error, malformed response)."""


class SessionFailed(SessionError):
    """Session ended in a failed/provision_failed/stopped state.

    Attributes:
        session_id: The Mimiry session ID for postmortem.
        state: Terminal state (``failed``, ``provision_failed``, ``stopped``).
        stop_reason: ``user_requested`` | ``timed_out`` | ``balance_depleted`` | None.
        events: Last events_tail payload (often the only place to see GCP capacity errors).
    """

    def __init__(
        self,
        message: str,
        *,
        session_id: str | None = None,
        state: str | None = None,
        stop_reason: str | None = None,
        events: list | None = None,
    ) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.state = state
        self.stop_reason = stop_reason
        self.events = events


class SessionTimeout(SessionError):
    """The session did not reach a terminal state within the configured timeout."""


class ResultParseError(MimiryError):
    """The remote function ran but the result sentinel was missing or unparseable."""
