"""Tests for abandoning a dead session instead of waiting out the SSH timeout.

When a container dies on startup its host goes with it, and every remaining
connect attempt times out. Without a state check the caller waits the full
five minutes and is finally told sshd is unreachable — a network diagnosis for
a container failure. These cover the check itself and its wiring into the
waiters.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from mimiry import _ssh
from mimiry._session import TERMINAL_STATES, make_terminal_check
from mimiry._ssh import SSHError, SshTarget, wait_for_sshd


class _FakeClient:
    def __init__(self, state=None, raises=False):
        self._state = state
        self._raises = raises
        self.calls = 0

    def get_session(self, session_id, *, events_tail=None):
        self.calls += 1
        if self._raises:
            raise RuntimeError("network blip")
        return {"id": session_id, "state": self._state}


# ────────────────────── make_terminal_check ──────────────────────


def test_reports_a_terminal_state():
    assert make_terminal_check(_FakeClient("terminated"), "s1")() == "terminated"


def test_reports_states_the_api_emits_that_a_hand_written_list_missed():
    # `exited` and `pull_failed` are emitted live and were absent from the
    # duplicated literal sets this check replaces.
    assert "exited" in TERMINAL_STATES and "pull_failed" in TERMINAL_STATES
    assert make_terminal_check(_FakeClient("exited"), "s1")() == "exited"
    assert make_terminal_check(_FakeClient("pull_failed"), "s1")() == "pull_failed"


def test_running_session_reports_nothing():
    assert make_terminal_check(_FakeClient("running"), "s1")() is None


def test_a_failed_poll_does_not_end_the_wait():
    # A convenience check must degrade, never raise: a network blip mid-wait
    # cannot be allowed to kill an otherwise healthy call.
    assert make_terminal_check(_FakeClient(raises=True), "s1")() is None


def test_state_is_matched_case_insensitively():
    assert make_terminal_check(_FakeClient("TERMINATED"), "s1")() == "terminated"


# ────────────────────── wait_for_sshd wiring ──────────────────────


def _target() -> SshTarget:
    return SshTarget(host="10.0.0.1", port=22, username="root", key_path=Path("/k"))


def test_wait_for_sshd_gives_up_when_the_session_is_already_dead(monkeypatch):
    attempts = {"n": 0}
    clock = {"t": 0.0}

    def _fake_run(*a, **kw):
        attempts["n"] += 1
        raise __import__("subprocess").TimeoutExpired(cmd="ssh", timeout=30)

    def _sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(_ssh.subprocess, "run", _fake_run)
    monkeypatch.setattr(_ssh.time, "sleep", _sleep)
    monkeypatch.setattr(_ssh.time, "monotonic", lambda: clock["t"])

    with pytest.raises(SSHError) as exc:
        wait_for_sshd(_target(), max_wait_seconds=300, terminal_check=lambda: "exited")

    msg = str(exc.value)
    assert "exited" in msg
    # The diagnosis must point at the container, not at the network.
    assert "container exited" in msg
    # And it must not have burned the whole timeout to say so.
    assert attempts["n"] == 1


def test_wait_for_sshd_keeps_waiting_while_the_session_lives(monkeypatch):
    calls = {"n": 0}
    clock = {"t": 0.0}

    class _R:
        returncode = 0
        stdout = b"ok"
        stderr = b""

    def _fake_run(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise __import__("subprocess").TimeoutExpired(cmd="ssh", timeout=30)
        return _R()

    def _sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(_ssh.subprocess, "run", _fake_run)
    monkeypatch.setattr(_ssh.time, "sleep", _sleep)
    monkeypatch.setattr(_ssh.time, "monotonic", lambda: clock["t"])

    wait_for_sshd(_target(), max_wait_seconds=300, terminal_check=lambda: None)
    assert calls["n"] == 3


def test_remote_flows_do_not_restate_the_terminal_state_names():
    # One definition only: a second copy is how `exited` got missed, and a
    # missed terminal state is a wait that never ends early.
    for name in ("mimiry.function", "mimiry.run"):
        assert '"provision_failed"' not in inspect.getsource(sys.modules[name])
