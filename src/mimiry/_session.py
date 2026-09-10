"""Session lifecycle: poll state, scan logs, surface terminal-state errors.

This is where v1 spends most of its wall-clock time. Today's API requires the
SDK to poll for state transitions and log content; Centrifugo SSE could replace
this in v2.

State model
-----------
These names are what the **live API actually emits**, confirmed by observing
real sessions end to end. They are not taken from the OpenAPI ``SessionState``
enum, which lists names the platform never sends (notably ``started``) and
omits most of the ones it does. Treating the spec as authoritative here made
the SDK wait for a state that never arrives, so every ready session looked
unreachable and every successful batch run was reported as a failure.

If the platform later changes what it emits, fix these constants — do not add
aliases. A second accepted spelling for one condition is what turned this into
a silent bug the first time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from mimiry._client import MimiryClient
from mimiry._config import Config
from mimiry.exceptions import SessionError, SessionFailed, SessionTimeout

#: The state in which the container is up and SSH is reachable.
READY_STATE = "running"

#: States the API emits while the session is still coming up. Purely
#: informational: anything not ready and not terminal is waited on.
PENDING_STATES = {
    "submitted",
    "provisioned",
    "booting",
    "setting_up",
    "pulling_image",
    "terminating",
}

#: States from which a session never recovers, as emitted by the live API.
#: ``exited`` is the container finishing its command; ``pull_failed`` is an
#: image that could not be fetched.
TERMINAL_STATES = {
    "terminated",
    "exited",
    "pull_failed",
    # Defined by the OpenAPI SessionState enum but never observed live. Kept
    # only so that a session reporting one is not polled until timeout — a
    # stalled poll leaves a paid instance running. Never treat these as the
    # expected spelling of anything.
    "completed",
    "failed",
    "stopped",
    "provision_failed",
}

#: Terminal states that mean the work did not run. Reaching a terminal state
#: is not by itself a failure: a batch session that ran its command and
#: auto-terminated ends in ``exited``/``terminated`` and is a success.
ERROR_STATES = {"failed", "provision_failed", "stopped", "pull_failed"}

#: Backwards-compatible alias. Prefer :data:`READY_STATE`.
RUNNING_STATE = READY_STATE


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
    """Poll until the session is ready or terminal. Returns (session_payload, timings)."""
    started_at = time.monotonic()
    last_state = None
    timings: dict[str, float] = {}
    deadline = started_at + config.timeout_seconds

    while True:
        if time.monotonic() > deadline:
            raise SessionTimeout(
                f"session {session_id} did not become ready within {config.timeout_seconds}s"
            )

        payload = client.get_session(session_id)
        state = _extract_state(payload)

        if state != last_state:
            timings[state] = time.monotonic() - started_at
            if on_state_change:
                on_state_change(state)
            last_state = state

        if state == READY_STATE:
            return payload, timings

        if state in TERMINAL_STATES:
            return payload, timings

        time.sleep(config.poll_interval_seconds)


def wait_for_ssh_ready(
    client: MimiryClient,
    session_id: str,
    config: Config,
    *,
    max_wait_seconds: int = 120,
    on_state_change: Callable[[str], None] | None = None,
) -> dict:
    """Once the session is ready, the ssh-proxy still needs a moment to publish
    the session's ``ssh.host``/``port``/``username``. Poll the session detail
    until ``ssh.host`` is non-empty (or a terminal state hits).

    Returns the session payload with ssh info filled in.
    """
    deadline = time.monotonic() + max_wait_seconds
    last_state: str | None = None
    while True:
        if time.monotonic() > deadline:
            raise SessionTimeout(
                f"session {session_id}: ssh.host not populated within {max_wait_seconds}s after becoming ready"
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


@dataclass
class RunInfo:
    """What one session cost and how long each phase took, from the platform's
    own numbers. Attached to a ``Function`` as ``last_run`` after every
    ``.remote()`` / ``.map()``, and to ``RunResult.info`` for ``mimiry.run``.

    ``phases`` is seconds from submission to the first sighting of each
    state, as the SDK observed them (so subject to its poll interval).
    ``final_cost`` is the platform's settled charge in ``currency`` once the
    session has terminated; ``None`` while it has not settled. ``duration``
    is submission to release, wall-clock, as seen by the SDK.
    """

    session_id: str
    gpu_type: str | None = None
    provider: str | None = None
    hourly_rate: float | None = None
    currency: str | None = None
    phases: dict[str, float] = field(default_factory=dict)
    duration: float | None = None
    final_cost: float | None = None
    state: str | None = None
    stop_reason: str | None = None


def build_run_info(
    client: MimiryClient,
    session_id: str,
    *,
    phases: dict[str, float],
    started_at: float,
    settle_wait_seconds: float = 6.0,
    poll_seconds: float = 1.0,
) -> RunInfo:
    """Read the session's billing block after release. The platform settles
    ``final_cost`` a few seconds after termination, so poll briefly; never
    raise — a missing figure is ``None``, not a failed call.
    """
    info = RunInfo(session_id=session_id, phases=dict(phases), duration=time.monotonic() - started_at)
    deadline = time.monotonic() + settle_wait_seconds
    while True:
        try:
            payload = client.get_session(session_id)
        except Exception:
            return info
        billing = payload.get("billing") or {}
        info.gpu_type = payload.get("gpu_type") or info.gpu_type
        info.provider = billing.get("provider") or info.provider
        info.hourly_rate = billing.get("hourly_rate") or info.hourly_rate
        info.currency = billing.get("currency") or info.currency
        info.state = _extract_state(payload) or info.state
        info.stop_reason = payload.get("stop_reason") or info.stop_reason
        cost = billing.get("final_cost")
        if cost is not None:
            info.final_cost = float(cost)
            return info
        if time.monotonic() >= deadline:
            return info
        time.sleep(poll_seconds)


def make_terminal_check(
    client: MimiryClient, session_id: str
) -> Callable[[], str | None]:
    """Return a callable that reports the session's state when it is terminal.

    Handed to the SSH waiters so they abandon a dead session instead of
    retrying against a machine that no longer exists. It reads
    :data:`TERMINAL_STATES` rather than restating the list: a second copy of
    the state names is how a state the API really emits (``exited``,
    ``pull_failed``) gets missed by one caller and honoured by another.

    Never raises — a failed poll returns ``None`` so the caller keeps waiting.
    """

    def _check() -> str | None:
        try:
            state = _extract_state(client.get_session(session_id)).lower()
        except Exception:
            return None
        return state if state in TERMINAL_STATES else None

    return _check


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

    # The platform's own ``error`` is the diagnosis when it has one — a
    # provisioning failure (no capacity, no candidate) never reached a
    # container, and blaming the user's command for it sends them debugging
    # the wrong thing.
    error = (session_payload.get("error") or "").strip()
    if error:
        msg = (
            f"session {session_id} ended in state={state} before the container "
            f"produced a result. Platform error: {error}"
        )
    else:
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


def preflight_volume_location(
    client: MimiryClient, mounts: list, requested_location: str | None
) -> str | None:
    """Reconcile the session's location with the locations of the volumes it
    mounts, returning the location to use (or ``None`` to leave it as-is).

    A volume lives in one location and the platform refuses to attach it
    anywhere else — but only after the session has been created, so the user
    watches a session appear and die instead of being told upfront. When no
    location was requested, the volume's own location is adopted; when one was
    requested and disagrees, the session is refused before it exists.

    Best-effort in one direction only: a definite mismatch raises, but any
    failure to *read* the volumes (network, unknown name, a payload without a
    location) leaves the request untouched and lets the platform decide.
    """
    names = [m.get("volume_name") for m in mounts if m.get("volume_name")]
    if not names:
        return None

    try:
        volumes = client.list_volumes()
    except Exception:
        return None

    by_name = {v.get("name"): v for v in volumes if isinstance(v, dict) and v.get("name")}
    located: dict[str, str] = {}
    for name in names:
        loc = (by_name.get(name) or {}).get("location")
        if loc:
            located[name] = loc

    if not located:
        return None

    distinct = set(located.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{n} in {loc}" for n, loc in sorted(located.items()))
        raise SessionError(
            f"the requested volumes are in different locations ({detail}); a session "
            f"runs in one location and can only mount volumes that live there. "
            f"Attach volumes from a single location, or create the missing one with "
            f"`mimiry volume create --location <location>`."
        )

    volume_location = distinct.pop()
    if requested_location and requested_location != volume_location:
        names_txt = ", ".join(sorted(located))
        raise SessionError(
            f"--location {requested_location} conflicts with volume {names_txt}, which "
            f"is in {volume_location}. A volume can only be mounted by a session in its "
            f"own location. Re-run with --location {volume_location}, or create a volume "
            f"in {requested_location} with `mimiry volume create --location "
            f"{requested_location}`."
        )
    return volume_location
