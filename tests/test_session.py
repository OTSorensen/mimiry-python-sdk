"""Tests for terminal-state failure surfacing (raise_if_ended_before_result)."""

from __future__ import annotations

import pytest

from mimiry._session import raise_if_ended_before_result
from mimiry.exceptions import SessionFailed


class _FakeClient:
    """Minimal client stub: returns canned logs/events for a terminated session."""

    def __init__(self, logs: str | None = None):
        self._logs = logs

    def get_logs(self, session_id, *, tail: int = 50, timestamps: bool = False):
        if self._logs is None:
            return {"_status": 409}  # logs window gone
        return {"_status": 200, "logs": self._logs}

    def get_session(self, session_id, *, events_tail=None):
        return {"events": []}


def test_no_raise_when_session_still_running():
    raise_if_ended_before_result({"id": "s", "state": "started"})  # no raise


def test_no_raise_when_provisioning():
    raise_if_ended_before_result({"id": "s", "state": "provisioned"})  # no raise


def test_raises_on_terminated_with_container_logs():
    # The exact failure we debugged: container exits 127 during bootstrap.
    client = _FakeClient(logs="bash: line 1: pip: command not found")
    with pytest.raises(SessionFailed) as exc:
        raise_if_ended_before_result(
            {"id": "s1", "state": "terminated", "stop_reason": "CONTAINER_EXITED"},
            client=client,
        )
    err = exc.value
    assert err.state == "terminated"
    assert err.stop_reason == "CONTAINER_EXITED"
    # The container's logs must be in the message so the user sees *why*.
    assert "pip: command not found" in str(err)


def test_raises_on_completed_even_without_client():
    # Any terminal state at this checkpoint means premature exit.
    with pytest.raises(SessionFailed):
        raise_if_ended_before_result({"id": "s", "state": "completed"})


def test_raises_gracefully_when_logs_unavailable():
    client = _FakeClient(logs=None)  # get_logs returns 409
    with pytest.raises(SessionFailed) as exc:
        raise_if_ended_before_result(
            {"id": "s2", "state": "stopped", "stop_reason": "timed_out"}, client=client
        )
    assert exc.value.state == "stopped"  # still raises, just without a log tail
