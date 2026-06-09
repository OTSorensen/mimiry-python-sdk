"""Tests for the CLI: session + volume management, availability, account, create/ssh.

The client and config are stubbed so no auth/network happens; we assert on
argument parsing, the calls made to the client, payloads built, and output.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import mimiry._cli as cli
from mimiry._config import Config

SESSIONS = [
    {"id": "aaaa1111", "state": "started", "operation": "", "name": "job-a",
     "created_at": "2026-06-08T12:00:00Z", "ssh": {"host": "1.2.3.4", "port": 22, "username": "root"}},
    {"id": "bbbb2222", "state": "terminated", "operation": None, "name": "job-b",
     "created_at": "2026-06-08T13:00:00Z"},
]
VOLUMES = [
    {"id": "vol-1", "state": "provisioned", "size_gb": 100, "attached_to": "", "name": "data",
     "created_at": "2026-06-08T12:00:00Z"},
    {"id": "vol-2", "state": "deleted", "size_gb": 50, "attached_to": None, "name": "old",
     "created_at": "2026-06-08T11:00:00Z"},
]


class _FakeClient:
    def __init__(self, **overrides):
        self._sessions = overrides.get("sessions", SESSIONS)
        self._session = overrides.get("session", {"id": "aaaa1111", "state": "started"})
        self._logs = overrides.get("logs", {"_status": 200, "logs": "line1\nline2\n"})
        self._terminate = overrides.get("terminate")
        self._balance = overrides.get("balance", {"balance": 49.95, "currency": "EUR"})
        self._availability = overrides.get("availability", {"gpu_models": []})
        self._transactions = overrides.get("transactions", {"transactions": []})
        self._volumes = overrides.get("volumes", VOLUMES)
        self._volume = overrides.get("volume", {"id": "vol-1", "state": "provisioned"})
        self._created = overrides.get("created", {"id": "new-sess", "state": "submitted"})
        self.calls: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # account
    def get_balance(self):
        return self._balance

    def get_availability(self, **params):
        self.calls["availability"] = params
        return self._availability

    def get_transactions(self, **params):
        self.calls["transactions"] = params
        return self._transactions

    # sessions
    def list_sessions(self, **params):
        self.calls["list_sessions"] = params
        return self._sessions

    def get_session(self, session_id, *, events_tail=None):
        return self._session

    def terminate_session(self, session_id):
        self.calls["terminated"] = session_id
        return self._terminate

    def get_logs(self, session_id, *, tail=200, timestamps=False):
        self.calls["logs"] = {"tail": tail, "timestamps": timestamps}
        return self._logs

    def create_session(self, payload):
        self.calls["create_session"] = payload
        return self._created

    # volumes
    def list_volumes(self, **params):
        self.calls["list_volumes"] = params
        return self._volumes

    def get_volume(self, volume_id):
        return self._volume

    def create_volume(self, payload):
        self.calls["create_volume"] = payload
        return {"id": "vol-new", "state": "submitted"}

    def extend_volume(self, volume_id, size_gb):
        self.calls["extend"] = {"id": volume_id, "size_gb": size_gb}
        return {"id": volume_id, "size_gb": size_gb}

    def delete_volume(self, volume_id):
        self.calls["deleted_volume"] = volume_id
        return self._terminate


@pytest.fixture
def patch_client(monkeypatch):
    def _install(fake):
        monkeypatch.setattr(cli, "_client", lambda: fake)
        return fake
    return _install


@pytest.fixture(autouse=True)
def patch_config(monkeypatch):
    """Deterministic, network-free config; fast polling for wait/follow."""
    cfg = Config(ssh_key_path=Path("/home/u/.ssh/mimiry"), api_base="https://api.test",
                 timeout_seconds=5, poll_interval_seconds=0.0)
    cfg.log_poll_interval_seconds = 0.0
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    return cfg


# ────────────────────────── version / account ──────────────────────────


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "mimiry" in capsys.readouterr().out


def test_config_no_network(capsys):
    assert cli.main(["config"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["api_base"] == "https://api.test"
    assert out["ssh_key_path"].endswith("/.ssh/mimiry")


def test_whoami(patch_client, capsys):
    patch_client(_FakeClient(balance={"balance": 10, "currency": "EUR"}))
    assert cli.main(["whoami"]) == 0
    assert "Authenticated" in capsys.readouterr().out


def test_transactions(patch_client, capsys):
    fake = patch_client(_FakeClient(transactions={"transactions": [{"amount": -1.2}]}))
    assert cli.main(["transactions", "--limit", "5"]) == 0
    assert fake.calls["transactions"] == {"limit": 5}


# ────────────────────────── availability filters ──────────────────────────


def test_availability_filters_passed_through(patch_client):
    fake = patch_client(_FakeClient())
    cli.main(["availability", "--gpu-family", "T4", "--provider", "gcp",
              "--location", "europe-west4-a", "--min-vram", "16", "--available-only"])
    p = fake.calls["availability"]
    assert p == {"gpu_family": "T4", "provider": "gcp", "location": "europe-west4-a",
                 "min_vram_gb": 16, "available_only": "true"}


# ────────────────────────── sessions: list / status / logs / terminate ──────────────────────────


def test_sessions_alias_lists_all(patch_client, capsys):
    fake = patch_client(_FakeClient())
    assert cli.main(["sessions"]) == 0
    assert "aaaa1111" in capsys.readouterr().out
    assert fake.calls["list_sessions"] == {}


def test_session_list_active_filters(patch_client):
    fake = patch_client(_FakeClient())
    assert cli.main(["session", "list", "--active"]) == 0
    assert "state_not" in fake.calls["list_sessions"]


def test_session_status_error_state_exit_1(patch_client):
    patch_client(_FakeClient(session={"id": "x", "state": "failed"}))
    assert cli.main(["session", "status", "x"]) == 1  # scriptable exit code


def test_session_status_ok_exit_0(patch_client):
    patch_client(_FakeClient(session={"id": "x", "state": "started"}))
    assert cli.main(["session", "status", "x"]) == 0


def test_session_logs_not_running(patch_client, capsys):
    patch_client(_FakeClient(logs={"_status": 409, "message": "not running"}))
    assert cli.main(["session", "logs", "x"]) == 1
    assert "not running" in capsys.readouterr().err.lower()


def test_session_terminate(patch_client, capsys):
    fake = patch_client(_FakeClient(terminate=None))
    assert cli.main(["session", "terminate", "aaaa1111"]) == 0
    assert fake.calls["terminated"] == "aaaa1111"


# ────────────────────────── sessions: create ──────────────────────────


def test_build_create_payload_full():
    args = argparse.Namespace(
        image="img:1", gpu="T4", gpu_count=2, provider="gcp", location="loc",
        command="python x.py", name="myjob", env=["A=1", "B=2"],
        volume=["data:/mnt/data"], auto_terminate=None, no_ssh=True,
    )
    p = cli._build_create_payload(args)
    assert p["image"] == {"uri": "img:1"}
    assert p["gpu"] == {"types": ["T4"], "count": 2, "provider": "gcp", "location": "loc"}
    assert p["command"] == "python x.py"
    assert p["environment_vars"] == {"A": "1", "B": "2"}
    assert p["volume_mounts"] == [{"volume_name": "data", "mount_path": "/mnt/data"}]
    assert p["auto_terminate"] == {"mode": "on_complete"}  # command present → on_complete
    assert p["ssh_enabled"] is False
    assert "ssh_public_key" not in p  # --no-ssh


def test_build_create_payload_interactive_defaults_never(monkeypatch):
    monkeypatch.setattr(cli, "_pubkey", lambda: "ssh-ed25519 AAAA test")
    args = argparse.Namespace(
        image="img", gpu="T4", gpu_count=1, provider=None, location=None,
        command=None, name=None, env=None, volume=None, auto_terminate=None, no_ssh=False,
    )
    p = cli._build_create_payload(args)
    assert p["auto_terminate"] == {"mode": "never"}  # no command → never
    assert p["ssh_public_key"] == "ssh-ed25519 AAAA test"
    assert "command" not in p


def test_build_create_payload_bad_env():
    args = argparse.Namespace(
        image="img", gpu="T4", gpu_count=1, provider=None, location=None,
        command="x", name=None, env=["NOEQUALS"], volume=None, auto_terminate=None, no_ssh=True,
    )
    with pytest.raises(RuntimeError, match="--env must be KEY=VALUE"):
        cli._build_create_payload(args)


def test_session_create_posts_and_prints(patch_client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_pubkey", lambda: "ssh-ed25519 AAAA test")
    fake = patch_client(_FakeClient(created={"id": "sess-9", "state": "submitted"}))
    rc = cli.main(["session", "create", "--image", "img:1", "--gpu", "T4", "--command", "echo hi"])
    assert rc == 0
    assert fake.calls["create_session"]["image"] == {"uri": "img:1"}
    assert "sess-9" in capsys.readouterr().out


# ────────────────────────── sessions: ssh ──────────────────────────


def test_build_ssh_argv():
    payload = {"ssh": {"host": "5.6.7.8", "port": 2222, "username": "root"}}
    argv = cli._build_ssh_argv(payload, Path("/k/mimiry"))
    assert argv[0] == "ssh"
    assert "5.6.7.8" in argv[-1] and argv[-1].startswith("root@")
    assert "-i" in argv and "/k/mimiry" in argv
    assert "2222" in argv  # port wired in


def test_session_ssh_execs_when_started(patch_client, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli.os, "execvp", lambda f, a: captured.update(file=f, argv=a))
    patch_client(_FakeClient(session={"id": "x", "state": "started",
                                      "ssh": {"host": "h", "port": 22, "username": "root"}}))
    cli.main(["session", "ssh", "x"])
    assert captured["file"] == "ssh"
    assert captured["argv"][-1] == "root@h"


def test_session_ssh_not_ready_returns_1(patch_client, capsys):
    patch_client(_FakeClient(session={"id": "x", "state": "provisioned", "ssh": {}}))
    assert cli.main(["session", "ssh", "x"]) == 1
    assert "not SSH-ready" in capsys.readouterr().err


# ────────────────────────── volumes ──────────────────────────


def test_volume_list_hides_deleted_by_default(patch_client, capsys):
    fake = patch_client(_FakeClient())
    assert cli.main(["volume", "list"]) == 0
    assert fake.calls["list_volumes"] == {"state_not": "deleted"}


def test_volume_list_all(patch_client):
    fake = patch_client(_FakeClient())
    assert cli.main(["volume", "list", "--all"]) == 0
    assert fake.calls["list_volumes"] == {}


def test_volume_create(patch_client, capsys):
    fake = patch_client(_FakeClient())
    assert cli.main(["volume", "create", "--name", "data", "--size-gb", "100",
                     "--provider", "gcp"]) == 0
    assert fake.calls["create_volume"] == {"name": "data", "size_gb": 100, "provider": "gcp"}


def test_volume_extend(patch_client):
    fake = patch_client(_FakeClient())
    assert cli.main(["volume", "extend", "vol-1", "--size-gb", "200"]) == 0
    assert fake.calls["extend"] == {"id": "vol-1", "size_gb": 200}


def test_volume_delete(patch_client, capsys):
    fake = patch_client(_FakeClient(terminate=None))
    assert cli.main(["volume", "delete", "vol-1"]) == 0
    assert fake.calls["deleted_volume"] == "vol-1"
    assert "Delete requested" in capsys.readouterr().out


# ────────────────────────── parsing guards ──────────────────────────


def test_session_without_subcommand_errors():
    with pytest.raises(SystemExit) as exc:
        cli.main(["session"])
    assert exc.value.code == 2


def test_volume_without_subcommand_errors():
    with pytest.raises(SystemExit) as exc:
        cli.main(["volume"])
    assert exc.value.code == 2


def test_volume_create_requires_size():
    with pytest.raises(SystemExit):
        cli.main(["volume", "create", "--name", "x"])  # missing --size-gb
