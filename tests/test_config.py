"""Tests for SDK config persistence + key-path resolution precedence."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

import mimiry._config as cfg_mod
from mimiry._config import (
    Config,
    _parse_simple_toml,
    config_path,
    get_config,
    read_key_path,
    save_key_path,
)


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Point XDG at a tmp dir and reset the process-global config per test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MIMIRY_SSH_KEY", raising=False)
    monkeypatch.delenv("MIMIRY_API_BASE", raising=False)
    # Reset the module-global Config so cached state doesn't leak across tests.
    monkeypatch.setattr(cfg_mod, "_config", Config())
    yield


def test_config_path_honours_xdg(tmp_path):
    assert config_path() == tmp_path / "xdg" / "mimiry" / "config.toml"


def test_save_creates_secure_file_and_dir():
    written = save_key_path("~/.ssh/mimiry")
    assert written.is_file()
    # File is 0600, parent dir is 0700.
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.parent.stat().st_mode) == 0o700
    # Stores the expanded path, not key material.
    text = written.read_text()
    assert str(Path("~/.ssh/mimiry").expanduser()) in text
    assert "PRIVATE KEY" not in text


def test_save_then_read_round_trips():
    save_key_path("/home/olive/.ssh/mimiry")
    assert read_key_path() == Path("/home/olive/.ssh/mimiry")


def test_read_missing_returns_none():
    assert read_key_path() is None


def test_read_rejects_group_or_other_writable():
    written = save_key_path("/home/olive/.ssh/mimiry")
    # Make it group+other writable — the injection vector we guard against.
    written.chmod(0o666)
    with pytest.raises(PermissionError, match="writable by group/other"):
        read_key_path()


def test_world_readable_is_allowed():
    """A path isn't secret; only writability is rejected."""
    written = save_key_path("/home/olive/.ssh/mimiry")
    written.chmod(0o644)  # readable by all, writable by none-but-owner
    assert read_key_path() == Path("/home/olive/.ssh/mimiry")


def test_precedence_explicit_beats_env_and_file(monkeypatch):
    save_key_path("/from/file")
    monkeypatch.setenv("MIMIRY_SSH_KEY", "/from/env")
    cfg_mod.configure(ssh_key_path="/from/arg")
    assert get_config().ssh_key_path == Path("/from/arg")


def test_precedence_env_beats_file(monkeypatch):
    save_key_path("/from/file")
    monkeypatch.setenv("MIMIRY_SSH_KEY", "/from/env")
    assert get_config().ssh_key_path == Path("/from/env")


def test_precedence_file_used_when_no_env_or_explicit():
    save_key_path("/from/file")
    assert get_config().ssh_key_path == Path("/from/file")


def test_get_config_none_when_nothing_set():
    assert get_config().ssh_key_path is None


def test_simple_toml_parser_fallback():
    text = (
        "# a comment\n"
        '\n'
        'ssh_key_path = "/home/olive/.ssh/mimiry"  # trailing\n'
    )
    assert _parse_simple_toml(text)["ssh_key_path"] == "/home/olive/.ssh/mimiry"


def test_simple_toml_parser_unescapes():
    assert _parse_simple_toml(r'ssh_key_path = "C:\\Users\\olive"')["ssh_key_path"] == r"C:\Users\olive"
