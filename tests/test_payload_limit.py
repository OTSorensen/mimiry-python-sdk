"""Tests for the payload soft-size limit warning in ``pack_call``.

The function payload rides in a container env var (a control-plane channel with
hard size ceilings), so an oversized blob would otherwise fail opaquely deep in
the env-var/SSH path. ``pack_call`` warns early but still proceeds.
"""

from __future__ import annotations

import warnings

import pytest

from mimiry._serialization import PAYLOAD_SOFT_LIMIT_BYTES, pack_call


def _noop(x: object) -> object:
    return x


def test_oversized_payload_warns_but_still_proceeds():
    # ~300 KB of args -> encoded payload exceeds the 256 KB soft limit.
    big = b"x" * 300_000
    with pytest.warns(UserWarning, match="soft limit") as record:
        encoded = pack_call(_noop, (big,), {})
    # Still returns a usable base64 payload (soft limit: warn, don't block).
    assert encoded and len(encoded) > PAYLOAD_SOFT_LIMIT_BYTES
    # The message must steer users to the right pattern for big data.
    assert "volume" in str(record[0].message) and "bucket" in str(record[0].message)


def test_small_payload_does_not_warn():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pack_call(_noop, ("just a short string",), {})
    assert not any(issubclass(w.category, UserWarning) for w in caught)
