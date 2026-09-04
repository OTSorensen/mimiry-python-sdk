"""Tests for the on-disk JWT cache (SDK-003).

Covers the cache's own behaviour and its integration with ``get_token``. The
central guarantee: a valid cache entry means **no** SSH signing and **no**
token-endpoint request, which is what stops sequential CLI calls from tripping
the live auth rate limit.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from mimiry import _token_cache

FP = "SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
OTHER_FP = "SHA256:ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
BASE = "https://alpha.mimiry.com"
HOUR = 3600.0


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """Redirect the cache at a tmp dir so tests never touch the real one."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    yield


def _future() -> float:
    return time.time() + HOUR


# ────────────────────────── round trip ──────────────────────────


def test_store_then_load_round_trips():
    _token_cache.store(FP, BASE, "jwt-abc", _future())
    loaded = _token_cache.load(FP, BASE)
    assert loaded is not None
    token, expires_at = loaded
    assert token == "jwt-abc"
    assert expires_at > time.time()


def test_load_missing_returns_none():
    assert _token_cache.load(FP, BASE) is None


def test_cache_dir_honours_xdg(tmp_path):
    assert _token_cache.cache_dir() == tmp_path / "cache" / "mimiry" / "tokens"


# ────────────────────────── key isolation ──────────────────────────


def test_different_fingerprint_does_not_hit():
    """A second SSH key must never receive the first key's token."""
    _token_cache.store(FP, BASE, "jwt-abc", _future())
    assert _token_cache.load(OTHER_FP, BASE) is None


def test_different_api_base_does_not_hit():
    """Switching hosts must not reuse a token minted for the old host."""
    _token_cache.store(FP, BASE, "jwt-abc", _future())
    assert _token_cache.load(FP, "https://staging.mimiry.com") is None


def test_trailing_slash_in_api_base_is_normalised():
    _token_cache.store(FP, BASE + "/", "jwt-abc", _future())
    assert _token_cache.load(FP, BASE) is not None


def test_filename_leaks_neither_fingerprint_nor_host():
    path = _token_cache.cache_path(FP, BASE)
    assert FP not in path.name
    assert "mimiry.com" not in path.name
    assert "/" not in path.stem


def test_payload_identity_mismatch_is_rejected():
    """A hand-copied file whose contents disagree with its name is refused."""
    path = _token_cache.cache_path(FP, BASE)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": "jwt-wrong-owner",
                "expires_at": _future(),
                "fingerprint": OTHER_FP,  # disagrees with the filename's key
                "api_base": BASE,
            }
        )
    )
    os.chmod(path, 0o600)
    assert _token_cache.load(FP, BASE) is None


# ────────────────────────── expiry ──────────────────────────


def test_expired_token_is_not_returned():
    _token_cache.store(FP, BASE, "jwt-old", time.time() - 1)
    assert _token_cache.load(FP, BASE) is None


def test_token_inside_expiry_margin_is_not_returned():
    """A token expiring in 60s is useless for a job about to start."""
    _token_cache.store(FP, BASE, "jwt-soon", time.time() + 60)
    assert _token_cache.load(FP, BASE) is None


def test_token_just_outside_margin_is_returned():
    _token_cache.store(FP, BASE, "jwt-ok", time.time() + _token_cache._EXPIRY_MARGIN_SECONDS + 30)
    assert _token_cache.load(FP, BASE) is not None


def test_expired_entry_is_deleted_on_read():
    _token_cache.store(FP, BASE, "jwt-old", time.time() - 1)
    path = _token_cache.cache_path(FP, BASE)
    assert path.is_file()
    _token_cache.load(FP, BASE)
    assert not path.exists()


def test_now_override_controls_expiry_decision():
    expires = time.time() + HOUR
    _token_cache.store(FP, BASE, "jwt-abc", expires)
    # Evaluated from a moment past expiry, the same entry is unusable.
    assert _token_cache.load(FP, BASE, now=expires + 1) is None


# ────────────────────────── permissions ──────────────────────────


def test_stored_file_is_0600_in_0700_dir():
    path = _token_cache.store(FP, BASE, "jwt-abc", _future())
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o644, 0o604, 0o660, 0o606])
def test_group_or_other_accessible_file_is_refused_and_removed(mode):
    """A readable token file is a leaked credential — refuse and delete it."""
    path = _token_cache.store(FP, BASE, "jwt-abc", _future())
    assert path is not None
    os.chmod(path, mode)
    assert _token_cache.load(FP, BASE) is None
    assert not path.exists()


def test_no_token_material_in_config_dir(tmp_path):
    """Tokens live in the cache dir, never beside the config file."""
    _token_cache.store(FP, BASE, "jwt-secret-value", _future())
    config_root = tmp_path / "xdg"
    if config_root.exists():
        for f in config_root.rglob("*"):
            if f.is_file():
                assert "jwt-secret-value" not in f.read_text()


# ────────────────────────── corruption tolerance ──────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "",
        "[]",  # valid JSON, wrong shape
        '{"expires_at": 99999999999}',  # missing access_token
        '{"access_token": "x"}',  # missing expires_at
        '{"access_token": "", "expires_at": 99999999999}',  # empty token
        '{"access_token": "x", "expires_at": "tomorrow"}',  # wrong type
    ],
)
def test_corrupt_entry_returns_none_without_raising(body):
    path = _token_cache.cache_path(FP, BASE)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(path, 0o600)
    assert _token_cache.load(FP, BASE) is None


def test_store_failure_is_swallowed(monkeypatch):
    """An unwritable cache dir must not break authentication."""

    def _boom(*_a, **_kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", _boom)
    assert _token_cache.store(FP, BASE, "jwt-abc", _future()) is None


# ────────────────────────── clear ──────────────────────────


def test_clear_removes_all_entries():
    _token_cache.store(FP, BASE, "a", _future())
    _token_cache.store(OTHER_FP, BASE, "b", _future())
    assert _token_cache.clear() == 2
    assert _token_cache.load(FP, BASE) is None
    assert _token_cache.load(OTHER_FP, BASE) is None


def test_clear_on_missing_dir_is_zero_not_error():
    assert _token_cache.clear() == 0


# ────────────────────────── get_token integration ──────────────────────────


@pytest.fixture
def fake_key(tmp_path, monkeypatch):
    """A key pair that exists on disk, with ssh-keygen fingerprinting stubbed."""
    from mimiry import _auth

    priv = tmp_path / "id_test"
    priv.write_text("PRIVATE")
    Path(f"{priv}.pub").write_text("ssh-ed25519 AAAA test")
    monkeypatch.setattr(_auth, "_fingerprint", lambda _pub: FP)
    return priv


def test_get_token_uses_cache_without_network(fake_key, monkeypatch):
    """The core SDK-003 guarantee: a cache hit performs no exchange at all."""
    from mimiry import _auth

    _token_cache.store(FP, BASE, "jwt-cached", _future())

    def _fail(*_a, **_kw):
        raise AssertionError("exchange_ssh_for_token must not be called on a cache hit")

    monkeypatch.setattr(_auth, "exchange_ssh_for_token", _fail)

    token = _auth.get_token(str(fake_key), BASE)
    assert token.access_token == "jwt-cached"
    assert token.fingerprint == FP
    assert token.api_base == BASE


def test_get_token_exchanges_on_cache_miss(fake_key, monkeypatch):
    from mimiry import _auth

    calls = []

    def _fake_exchange(key, base):
        calls.append((str(key), base))
        return _auth.Token("jwt-fresh", time.time() + HOUR, FP, Path(key), base)

    monkeypatch.setattr(_auth, "exchange_ssh_for_token", _fake_exchange)

    token = _auth.get_token(str(fake_key), BASE)
    assert token.access_token == "jwt-fresh"
    assert len(calls) == 1


def test_get_token_use_cache_false_forces_exchange(fake_key, monkeypatch):
    from mimiry import _auth

    _token_cache.store(FP, BASE, "jwt-cached", _future())
    calls = []

    def _fake_exchange(key, base):
        calls.append(base)
        return _auth.Token("jwt-fresh", time.time() + HOUR, FP, Path(key), base)

    monkeypatch.setattr(_auth, "exchange_ssh_for_token", _fake_exchange)

    token = _auth.get_token(str(fake_key), BASE, use_cache=False)
    assert token.access_token == "jwt-fresh"
    assert len(calls) == 1


def test_get_token_falls_through_when_key_missing(monkeypatch, tmp_path):
    """An unidentifiable key must surface the real error, not a cache miss."""
    from mimiry import _auth
    from mimiry.exceptions import AuthError

    with pytest.raises(AuthError, match="not found"):
        _auth.get_token(str(tmp_path / "nope"), BASE)


def test_sequential_get_token_calls_exchange_once(fake_key, monkeypatch):
    """Simulates the reported bug: N CLI invocations, one exchange total."""
    from mimiry import _auth

    exchanges = []

    def _fake_exchange(key, base):
        exchanges.append(base)
        expires_at = time.time() + HOUR
        _token_cache.store(FP, base, "jwt-fresh", expires_at)
        return _auth.Token("jwt-fresh", expires_at, FP, Path(key), base)

    monkeypatch.setattr(_auth, "exchange_ssh_for_token", _fake_exchange)

    for _ in range(20):
        assert _auth.get_token(str(fake_key), BASE).access_token == "jwt-fresh"

    assert len(exchanges) == 1, f"expected 1 exchange across 20 calls, got {len(exchanges)}"
