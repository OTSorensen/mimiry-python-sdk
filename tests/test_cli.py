"""Tests for the session-management CLI commands.

The client is stubbed so no auth/network happens; we assert on argument parsing,
the calls made to the client, and the rendered output.
"""

from __future__ import annotations

import json

import pytest

import mimiry._cli as cli

SESSIONS = [
    {"id": "aaaa1111", "state": "started", "operation": "", "name": "job-a",
     "created_at": "2026-06-08T12:00:00Z"},
    {"id": "bbbb2222", "state": "terminated", "operation": None, "name": "job-b",
     "created_at": "2026-06-08T13:00:00Z"},
]


class _FakeClient:
    def __init__(self, *, sessions=None, session=None, logs=None, terminate=None):
        self._sessions = sessions if sessions is not None else SESSIONS
        self._session = session or {"id": "aaaa1111", "state": "started"}
        self._logs = logs or {"_status": 200, "logs": "line1\nline2\n"}
        self._terminate = terminate
        self.list_params = None
        self.terminated_id = None
        self.logs_kwargs = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_sessions(self, **params):
        self.list_params = params
        return self._sessions

    def get_session(self, session_id, *, events_tail=None):
        self._session["_requested_events"] = events_tail
        return self._session

    def terminate_session(self, session_id):
        self.terminated_id = session_id
        return self._terminate

    def get_logs(self, session_id, *, tail=200, timestamps=False):
        self.logs_kwargs = {"tail": tail, "timestamps": timestamps}
        return self._logs


@pytest.fixture
def patch_client(monkeypatch):
    """Patch cli._client to return a provided fake, and return the fake."""
    def _install(fake):
        monkeypatch.setattr(cli, "_client", lambda: fake)
        return fake
    return _install


# ────────────────────────── list / sessions alias ──────────────────────────


def test_sessions_alias_lists_all(patch_client, capsys):
    fake = patch_client(_FakeClient())
    assert cli.main(["sessions"]) == 0
    out = capsys.readouterr().out
    assert "aaaa1111" in out and "bbbb2222" in out
    assert fake.list_params == {}  # no filter


def test_session_list_active_filters(patch_client, capsys):
    fake = patch_client(_FakeClient())
    assert cli.main(["session", "list", "--active"]) == 0
    assert "state_not" in fake.list_params  # active → exclude terminal states


def test_session_list_json(patch_client, capsys):
    patch_client(_FakeClient())
    assert cli.main(["session", "list", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in parsed] == ["aaaa1111", "bbbb2222"]


def test_session_list_empty(patch_client, capsys):
    patch_client(_FakeClient(sessions=[]))
    assert cli.main(["sessions", "--active"]) == 0
    assert "No active sessions." in capsys.readouterr().out


def test_list_is_newest_first(patch_client, capsys):
    patch_client(_FakeClient())
    cli.main(["sessions"])
    out = capsys.readouterr().out
    # bbbb (13:00) should appear before aaaa (12:00).
    assert out.index("bbbb2222") < out.index("aaaa1111")


# ────────────────────────── status / terminate / logs ──────────────────────────


def test_session_status(patch_client, capsys):
    patch_client(_FakeClient(session={"id": "aaaa1111", "state": "started"}))
    assert cli.main(["session", "status", "aaaa1111"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "aaaa1111"


def test_session_terminate(patch_client, capsys):
    fake = patch_client(_FakeClient(terminate=None))  # 202/204 → None
    assert cli.main(["session", "terminate", "aaaa1111"]) == 0
    assert fake.terminated_id == "aaaa1111"
    assert "Terminated aaaa1111." in capsys.readouterr().out


def test_session_logs(patch_client, capsys):
    fake = patch_client(_FakeClient(logs={"_status": 200, "logs": "hello\nworld\n"}))
    assert cli.main(["session", "logs", "aaaa1111", "--tail", "50"]) == 0
    assert fake.logs_kwargs["tail"] == 50
    assert capsys.readouterr().out.strip() == "hello\nworld"


def test_session_logs_not_running_returns_1(patch_client, capsys):
    patch_client(_FakeClient(logs={"_status": 409, "message": "not running"}))
    assert cli.main(["session", "logs", "aaaa1111"]) == 1
    assert "not running" in capsys.readouterr().err.lower()


# ────────────────────────── parsing guards ──────────────────────────


def test_session_without_subcommand_errors(patch_client):
    # argparse exits 2 when a required subcommand is missing.
    with pytest.raises(SystemExit) as exc:
        cli.main(["session"])
    assert exc.value.code == 2
