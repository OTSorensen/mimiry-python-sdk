"""Tests for the HMAC-signed result envelope.

The container wraps its cloudpickled return value as ``<hex>\\n<base64>`` and
signs it with a per-call secret; the SDK verifies the HMAC before it ever calls
``cloudpickle.loads``. These tests cover the verify side and assert the
container-side bootstrap actually emits the signing step.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import cloudpickle
import pytest

from mimiry._serialization import (
    build_bootstrap_script,
    new_result_hmac_key,
    parse_result,
    result_hmac_env_var,
    verify_result_envelope,
)
from mimiry.exceptions import ResultIntegrityError


def _make_envelope(value: object, key: str) -> tuple[str, str]:
    """Replicate the container's signing exactly, so drift in either side fails a test.

    Returns ``(envelope, inner_b64)``.
    """
    payload = {"ok": True, "result": value}
    wire = base64.b64encode(cloudpickle.dumps(payload)).decode("ascii")
    sig = hmac.new(key.encode("ascii"), wire.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{sig}\n{wire}", wire


# ────────────────────────── key generation ──────────────────────────


def test_new_key_is_256_bit_hex_and_unique():
    a, b = new_result_hmac_key(), new_result_hmac_key()
    assert len(a) == 64 and len(b) == 64  # 32 bytes -> 64 hex chars
    assert all(c in "0123456789abcdef" for c in a)
    assert a != b


# ────────────────────────── happy path ──────────────────────────


def test_valid_envelope_verifies_and_round_trips():
    key = new_result_hmac_key()
    envelope, inner = _make_envelope({"loss": 0.1, "n": 42}, key)
    assert verify_result_envelope(envelope, key) == inner
    assert parse_result(verify_result_envelope(envelope, key)) == {"loss": 0.1, "n": 42}


# ────────────────────────── rejection paths ──────────────────────────


def test_tampered_payload_is_rejected_before_unpickle():
    key = new_result_hmac_key()
    envelope, _ = _make_envelope("benign", key)
    sig, _, body = envelope.partition("\n")
    # Flip one character of the (still-valid-base64) body; HMAC must catch it.
    forged_body = ("A" if body[0] != "A" else "B") + body[1:]
    with pytest.raises(ResultIntegrityError):
        verify_result_envelope(f"{sig}\n{forged_body}", key)


def test_wrong_key_is_rejected():
    envelope, _ = _make_envelope("x", new_result_hmac_key())
    with pytest.raises(ResultIntegrityError):
        verify_result_envelope(envelope, new_result_hmac_key())


def test_forged_signature_is_rejected():
    key = new_result_hmac_key()
    _, body = _make_envelope("x", key)
    with pytest.raises(ResultIntegrityError):
        verify_result_envelope(f"{'0' * 64}\n{body}", key)


def test_missing_header_is_rejected():
    with pytest.raises(ResultIntegrityError):
        verify_result_envelope("no-newline-here", new_result_hmac_key())


# ────────────────────────── container-side wiring ──────────────────────────


def _decode_embedded_python(bootstrap: str) -> str:
    """Pull the base64-encoded Python bootstrap back out of the bash wrapper."""
    import re

    m = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", bootstrap)
    assert m, "could not locate the embedded base64 Python in the bootstrap"
    return base64.b64decode(m.group(1)).decode()


def test_bootstrap_emits_hmac_signing_step():
    py = _decode_embedded_python(build_bootstrap_script())
    # The container must read the per-call key and sign with hmac-sha256.
    assert result_hmac_env_var() in py
    assert "hmac.new" in py
    assert "hashlib.sha256" in py
    # And it must write the signed envelope, not the bare payload.
    assert "_envelope" in py


def test_embedded_bootstrap_python_compiles():
    # Guard against a syntax error sneaking into the embedded script.
    py = _decode_embedded_python(build_bootstrap_script())
    compile(py, "<mimiry-bootstrap>", "exec")
