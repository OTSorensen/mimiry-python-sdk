"""Tests for the interactive setup wizard (``mimiry setup`` / ``mimiry init``).

All external effects are mocked: ``ssh-keygen`` is never invoked for real, no
network call is made, and the user's real home / rc files are never touched.
Prompts are driven by monkeypatching ``_setup._prompt`` / ``_setup._confirm``
or by feeding ``input`` via ``monkeypatch.setattr('builtins.input', ...)``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import mimiry._setup as setup


# ────────────────────────── prompt helpers ──────────────────────────


def test_prompt_returns_default_on_eof(monkeypatch):
    def boom(_msg):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert setup._prompt("anything? ", default="fallback") == "fallback"


def test_prompt_strips_and_falls_back_to_default_on_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _msg: "   ")
    assert setup._prompt("q? ", default="d") == "d"
    monkeypatch.setattr("builtins.input", lambda _msg: "  hello  ")
    assert setup._prompt("q? ", default="d") == "hello"


@pytest.mark.parametrize(
    ("answer", "default", "expected"),
    [
        ("", True, True),  # empty → default
        ("", False, False),
        ("y", False, True),
        ("yes", False, True),
        ("n", True, False),
        ("nope", True, False),  # anything not in {y,yes} is False
    ],
)
def test_confirm(monkeypatch, answer, default, expected):
    monkeypatch.setattr(setup, "_prompt", lambda _msg: answer)
    assert setup._confirm("ok?", default=default) is expected


# ────────────────────────── _ensure_key ──────────────────────────


def _fake_run_factory(record):
    """Return a fake ``subprocess.run`` that records calls and fakes ssh-keygen.

    For ``ssh-keygen -y`` (derive pubkey) it writes a dummy line to the passed
    ``stdout`` file handle; for keypair generation it creates the priv/pub files.
    """

    def fake_run(cmd, *args, **kwargs):
        record.append(cmd)
        if "-y" in cmd:  # derive public key from private key → stdout
            kwargs["stdout"].write("ssh-ed25519 AAAAfake derived\n")
        elif "-t" in cmd:  # generate a new keypair at -f <path>
            target = Path(cmd[cmd.index("-f") + 1])
            target.write_text("PRIVATE\n")
            Path(f"{target}.pub").write_text("ssh-ed25519 AAAAfake mimiry\n")
        return subprocess.CompletedProcess(cmd, 0)

    return fake_run


def test_ensure_key_generates_when_missing(tmp_path, monkeypatch, capsys):
    calls: list = []
    monkeypatch.setattr(setup.subprocess, "run", _fake_run_factory(calls))
    key = tmp_path / "subdir" / "mimiry"

    result = setup._ensure_key(key)

    assert result == key
    assert key.is_file() and Path(f"{key}.pub").is_file()
    # Parent dir created with 0700.
    assert (key.parent.stat().st_mode & 0o777) == 0o700
    # ssh-keygen was asked for an ed25519 key at the right path.
    assert calls and calls[0][:3] == ["ssh-keygen", "-t", "ed25519"]
    assert str(key) in calls[0]


def test_ensure_key_uses_existing_keypair_without_regenerating(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(setup.subprocess, "run", _fake_run_factory(calls))
    key = tmp_path / "mimiry"
    key.write_text("PRIVATE\n")
    Path(f"{key}.pub").write_text("ssh-ed25519 AAAA existing\n")

    assert setup._ensure_key(key) == key
    assert calls == []  # no ssh-keygen invocation at all


def test_ensure_key_derives_missing_pub_from_existing_priv(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(setup.subprocess, "run", _fake_run_factory(calls))
    key = tmp_path / "mimiry"
    key.write_text("PRIVATE\n")  # priv present, .pub absent

    setup._ensure_key(key)

    pub = Path(f"{key}.pub")
    assert pub.is_file()
    assert pub.read_text() == "ssh-ed25519 AAAAfake derived\n"
    # Used the "-y" derive path, not a full keygen.
    assert calls and "-y" in calls[0]


# ────────────────────────── _detect_rc ──────────────────────────


@pytest.mark.parametrize(
    ("shell", "rc_name", "needle"),
    [
        ("/usr/bin/zsh", ".zshrc", "export MIMIRY_SSH_KEY="),
        ("/usr/bin/bash", ".bashrc", "export MIMIRY_SSH_KEY="),
        ("/usr/bin/fish", "config.fish", "set -x MIMIRY_SSH_KEY "),
        ("", ".bashrc", "export MIMIRY_SSH_KEY="),  # unknown → bash default
    ],
)
def test_detect_rc(monkeypatch, tmp_path, shell, rc_name, needle):
    monkeypatch.setenv("SHELL", shell)
    monkeypatch.setattr(setup.Path, "home", classmethod(lambda cls: tmp_path))
    rc_path, template = setup._detect_rc()
    assert rc_path.name == rc_name
    assert needle in template


# ────────────────────────── _write_rc ──────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point ``Path.home()`` at a tmp dir and default the shell to bash."""
    monkeypatch.setattr(setup.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    return tmp_path


def test_write_rc_appends_export_with_marker(home, monkeypatch):
    monkeypatch.setattr(setup, "_confirm", lambda *a, **k: True)
    key = Path("/home/olive/.ssh/mimiry")

    setup._write_rc(key)

    rc = (home / ".bashrc").read_text()
    assert setup._RC_MARKER in rc
    assert f"export MIMIRY_SSH_KEY={key}" in rc


def test_write_rc_is_idempotent_when_line_present(home, monkeypatch):
    key = Path("/home/olive/.ssh/mimiry")
    rc = home / ".bashrc"
    rc.write_text(f"export MIMIRY_SSH_KEY={key}\n")
    # Should not even ask to confirm — fail loudly if it does.
    monkeypatch.setattr(setup, "_confirm", lambda *a, **k: pytest.fail("should not prompt"))

    setup._write_rc(key)

    # Unchanged — no duplicate marker line added.
    assert rc.read_text() == f"export MIMIRY_SSH_KEY={key}\n"


def test_write_rc_respects_decline(home, monkeypatch):
    monkeypatch.setattr(setup, "_confirm", lambda *a, **k: False)
    rc = home / ".bashrc"
    rc.write_text("# pre-existing\n")

    setup._write_rc(Path("/home/olive/.ssh/mimiry"))

    assert "MIMIRY_SSH_KEY" not in rc.read_text()


def test_write_rc_warns_on_conflicting_value_and_can_skip(home, monkeypatch):
    rc = home / ".bashrc"
    rc.write_text("export MIMIRY_SSH_KEY=/old/key\n")
    # First _confirm = "append anyway?" → decline, so nothing else happens.
    monkeypatch.setattr(setup, "_confirm", lambda *a, **k: False)

    setup._write_rc(Path("/new/key"))

    assert "/new/key" not in rc.read_text()
    assert "/old/key" in rc.read_text()


# ────────────────────────── _verify ──────────────────────────


class _FakeClient:
    def __init__(self, balance):
        self._balance = balance

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_balance(self):
        return self._balance


def test_verify_success(monkeypatch, capsys):
    monkeypatch.setattr(setup, "get_token", lambda key, base: "tok")
    monkeypatch.setattr(setup, "MimiryClient", lambda tok: _FakeClient({"balance": 42, "currency": "EUR"}))

    assert setup._verify(Path("/k"), "https://api") is True
    out = capsys.readouterr().out
    assert "42 EUR" in out


def test_verify_failure_returns_false_with_guidance(monkeypatch, capsys):
    def boom(_key, _base):
        raise RuntimeError("key not registered")

    monkeypatch.setattr(setup, "get_token", boom)

    assert setup._verify(Path("/k"), "https://api") is False
    out = capsys.readouterr().out
    assert "Balance check failed" in out
    assert "register" in out.lower()


# ────────────────────────── run_setup_wizard orchestration ──────────────────────────


def test_run_setup_wizard_success(monkeypatch, tmp_path):
    """End-to-end happy path with every side-effecting step stubbed."""
    priv = tmp_path / "mimiry"
    priv.write_text("PRIVATE\n")
    Path(f"{priv}.pub").write_text("ssh-ed25519 AAAA mimiry\n")

    monkeypatch.setattr(setup, "_ensure_key", lambda kp: priv)
    monkeypatch.setattr(setup, "_register_key", lambda pub, base: None)
    monkeypatch.setattr(setup, "save_key_path", lambda p: tmp_path / "config.toml")
    monkeypatch.setattr(setup, "_write_rc", lambda p: None)
    monkeypatch.setattr(setup, "_verify", lambda p, b: True)
    monkeypatch.delenv("MIMIRY_SSH_KEY", raising=False)

    code = setup.run_setup_wizard(ssh_key_path=priv, api_base="https://api")

    assert code == 0
    # The wizard exports the key for the current process (used by verify).
    assert setup.os.environ["MIMIRY_SSH_KEY"] == str(priv)


def test_run_setup_wizard_returns_1_when_verify_fails(monkeypatch, tmp_path):
    priv = tmp_path / "mimiry"
    monkeypatch.setattr(setup, "_ensure_key", lambda kp: priv)
    monkeypatch.setattr(setup, "_register_key", lambda pub, base: None)
    monkeypatch.setattr(setup, "save_key_path", lambda p: tmp_path / "config.toml")
    monkeypatch.setattr(setup, "_write_rc", lambda p: None)
    monkeypatch.setattr(setup, "_verify", lambda p, b: False)

    assert setup.run_setup_wizard(ssh_key_path=priv, api_base="https://api") == 1
