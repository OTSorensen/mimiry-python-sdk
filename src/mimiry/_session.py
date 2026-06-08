"""Session lifecycle: poll state, scan logs, surface terminal-state errors.

This is where v1 spends most of its wall-clock time. Today's API requires the
SDK to poll for state transitions and log content; Centrifugo SSE could replace
this in v2.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from mimiry._client import MimiryClient
from mimiry._config import Config
from mimiry.exceptions import SessionFailed, SessionTimeout

TERMINAL_STATES = {"completed", "terminated", "failed", "stopped", "provision_failed"}
ERROR_STATES = {"failed", "provision_failed", "stopped"}
RUNNING_STATE = "started"  # Mimiry's state for "container running"


@dataclass
class SessionRun:
    """A completed (or failed) session run, with everything the caller might want."""

    session_id: str
    state: str
    stop_reason: str | None
    logs: str
    raw: dict  # the final session payload from the API
    timings: dict[str, float]  # phase → seconds


def _extract_state(payload: dict) -> str:
    """The April 2026 report flagged that ``status`` / ``operation`` are absent on terminal
    states. The API's durable field is ``state``; we prefer it and fall back to ``status``.
    """
    return payload.get("state") or payload.get("status") or "unknown"


def wait_for_started_or_terminal(
    client: MimiryClient,
    session_id: str,
    config: Config,
    on_state_change: Callable[[str], None] | None = None,
) -> tuple[dict, dict[str, float]]:
    """Poll until ``state=started`` or any terminal state. Returns (session_payload, timings)."""
    started_at = time.monotonic()
    last_state = None
    timings: dict[str, float] = {}
    deadline = started_at + config.timeout_seconds

    while True:
        if time.monotonic() > deadline:
            raise SessionTimeout(
                f"session {session_id} did not reach started within {config.timeout_seconds}s"
            )

        payload = client.get_session(session_id)
        state = _extract_state(payload)

        if state != last_state:
            timings[state] = time.monotonic() - started_at
            if on_state_change:
                on_state_change(state)
            last_state = state

        if state == RUNNING_STATE:
            return payload, timings

        if state in TERMINAL_STATES:
            return payload, timings

        time.sleep(config.poll_interval_seconds)


def wait_for_marker(
    client: MimiryClient,
    session_id: str,
    marker: str,
    config: Config,
    *,
    state_at_start: str = RUNNING_STATE,
    log_tail: int = 1000,
) -> tuple[str, dict, dict[str, float]]:
    """Poll logs until ``marker`` appears OR the session reaches a terminal state.

    Returns ``(logs, final_session_payload, timings)``. ``logs`` may not contain
    ``marker`` if the session terminated before the marker was emitted — caller
    must check.
    """
    started_at = time.monotonic()
    deadline = started_at + config.timeout_seconds
    last_state = state_at_start
    last_logs = ""
    final_payload: dict = {}
    timings: dict[str, float] = {}

    while True:
        if time.monotonic() > deadline:
            raise SessionTimeout(
                f"session {session_id} did not emit marker within {config.timeout_seconds}s"
            )

        # Try to grab logs. If 503, the container is still pulling — back off.
        log_resp = client.get_logs(session_id, tail=log_tail)
        if log_resp.get("_status") == 200:
            last_logs = log_resp.get("logs", "") or ""
            if marker in last_logs:
                final_payload = client.get_session(session_id)
                timings["marker_found"] = time.monotonic() - started_at
                return last_logs, final_payload, timings
        elif log_resp.get("_status") == 503:
            wait = float(log_resp.get("retry_after_seconds", config.log_poll_interval_seconds))
            time.sleep(min(wait, 30))
            continue
        elif log_resp.get("_status") == 409:
            # Container not running anymore — session probably terminated. Fall through to state
            # check below to confirm.
            pass

        # Check state — exit when terminal.
        payload = client.get_session(session_id)
        state = _extract_state(payload)
        if state != last_state:
            timings[state] = time.monotonic() - started_at
            last_state = state

        if state in TERMINAL_STATES:
            # One last-ditch log fetch (sometimes the marker arrives in the same tick as
            # auto-terminate). If it 409s, we've lost the logs window — return what we have.
            final_resp = client.get_logs(session_id, tail=log_tail)
            if final_resp.get("_status") == 200:
                last_logs = final_resp.get("logs", "") or last_logs
            return last_logs, payload, timings

        time.sleep(config.log_poll_interval_seconds)


def wait_for_ssh_ready(
    client: MimiryClient,
    session_id: str,
    config: Config,
    *,
    max_wait_seconds: int = 120,
    on_state_change: Callable[[str], None] | None = None,
) -> dict:
    """After ``state=started``, the ssh-proxy still needs a moment to publish
    the session's ``ssh.host``/``port``/``username``. Poll the session detail
    until ``ssh.host`` is non-empty (or a terminal state hits).

    Returns the session payload with ssh info filled in.
    """
    deadline = time.monotonic() + max_wait_seconds
    last_state: str | None = None
    while True:
        if time.monotonic() > deadline:
            raise SessionTimeout(
                f"session {session_id}: ssh.host not populated within {max_wait_seconds}s after state=started"
            )
        payload = client.get_session(session_id)
        state = _extract_state(payload)
        if state != last_state and on_state_change:
            on_state_change(state)
            last_state = state
        if state in TERMINAL_STATES:
            return payload
        ssh = payload.get("ssh") or {}
        if ssh.get("host") and ssh.get("port"):
            return payload
        time.sleep(min(config.poll_interval_seconds, 3.0))


def fetch_events(client: MimiryClient, session_id: str) -> list:
    """Fetch the session's full event history. Useful on failure (GCP capacity, etc.)."""
    payload = client.get_session(session_id, events_tail=-1)
    return payload.get("events") or []


def raise_if_failed(session_payload: dict, client: MimiryClient | None = None) -> None:
    """Raise SessionFailed if the session is in an error state."""
    state = _extract_state(session_payload)
    if state in ERROR_STATES:
        session_id = session_payload.get("id", "?")
        events = None
        if client is not None:
            try:
                events = fetch_events(client, session_id)
            except Exception:
                events = None
        raise SessionFailed(
            f"session {session_id} ended in state={state} "
            f"(stop_reason={session_payload.get('stop_reason')})",
            session_id=session_id,
            state=state,
            stop_reason=session_payload.get("stop_reason"),
            events=events,
        )


def raise_if_ended_before_result(
    session_payload: dict, client: MimiryClient | None = None
) -> None:
    """Raise SessionFailed if the session reached **any** terminal state at a point
    where the container was expected to still be running (e.g. blocking on the
    result/done flag).

    In v1 the container writes its result and then blocks until the SDK signals
    done, so reaching ``terminated``/``completed``/``failed`` at the just-started
    checkpoint means the command exited prematurely — almost always a bootstrap
    failure (bad image, failed pip install, etc.). We surface the tail of the
    container logs so the caller sees *why* instead of timing out on SSH.
    """
    state = _extract_state(session_payload)
    if state not in TERMINAL_STATES:
        return

    session_id = session_payload.get("id", "?")
    stop_reason = session_payload.get("stop_reason")

    tail = ""
    events = None
    if client is not None:
        try:
            resp = client.get_logs(session_id, tail=50)
            if resp.get("_status") == 200:
                tail = (resp.get("logs") or "").strip()
        except Exception:
            pass
        try:
            events = fetch_events(client, session_id)
        except Exception:
            events = None

    msg = (
        f"session {session_id} ended in state={state} (stop_reason={stop_reason}) "
        f"before the container produced a result — the command likely failed during "
        f"startup"
    )
    if tail:
        msg += f". Last container logs:\n{tail}"
    raise SessionFailed(
        msg,
        session_id=session_id,
        state=state,
        stop_reason=stop_reason,
        events=events,
    )
