"""Tests for pre-create GPU/provider/location validation against /availability."""

from __future__ import annotations

import pytest

from mimiry._availability import check_gpu_offered, preflight_gpu_availability
from mimiry.exceptions import SessionError

# Mirrors the shape of GET /api/compute/v1/availability (abbreviated).
SAMPLE = {
    "gpu_models": [
        {
            "name": "T4", "family": "T4", "available": True, "currency": "EUR",
            "providers": [{"provider": "gcp", "hourly_rate": 0.36, "locations": ["europe-west4-a"]}],
        },
        {
            "name": "H100_SXM", "family": "H100", "available": True, "currency": "EUR",
            "providers": [
                {"provider": "verda", "hourly_rate": 2.1, "locations": ["FIN-01"]},
                {"provider": "acme", "hourly_rate": 2.5, "locations": ["US-EAST-1"]},
            ],
        },
        {
            "name": "A100", "family": "A100", "available": False, "currency": "EUR",
            "providers": [{"provider": "verda", "hourly_rate": 1.2, "locations": ["FIN-01"]}],
        },
    ]
}
MODELS = SAMPLE["gpu_models"]


# ────────────────────────── check_gpu_offered (pure) ──────────────────────────


def test_ok_when_gpu_and_provider_match():
    check_gpu_offered(MODELS, "T4", "gcp", None)  # no raise


def test_ok_when_no_provider_hint():
    check_gpu_offered(MODELS, "T4", None, None)  # available somewhere → fine


def test_raises_when_provider_does_not_offer_gpu():
    # The real bug: T4 requested from verda, which only offers H100/A100.
    with pytest.raises(SessionError, match="not offered by provider 'verda'") as exc:
        check_gpu_offered(MODELS, "T4", "verda", None)
    # The error must point the user at the provider that *does* offer it.
    assert "gcp" in str(exc.value)


def test_raises_when_gpu_type_unknown():
    with pytest.raises(SessionError, match="not offered"):
        check_gpu_offered(MODELS, "B200", None, None)


def test_raises_when_gpu_unavailable_everywhere():
    with pytest.raises(SessionError, match="currently unavailable"):
        check_gpu_offered(MODELS, "A100", None, None)


def test_ok_matching_by_family_name():
    # "H100" is a family; the offered model name is "H100_SXM".
    check_gpu_offered(MODELS, "H100", "acme", None)
    check_gpu_offered(MODELS, "H100_SXM", "verda", None)


def test_ok_when_location_matches():
    check_gpu_offered(MODELS, "T4", "gcp", "europe-west4-a")


def test_raises_when_location_not_offered_by_provider():
    with pytest.raises(SessionError, match="not in location 'us-central1'") as exc:
        check_gpu_offered(MODELS, "T4", "gcp", "us-central1")
    assert "europe-west4-a" in str(exc.value)


# ────────────────────────── preflight wrapper (I/O, best-effort) ──────────────────────────


class _FakeClient:
    def __init__(self, *, data=None, raises: Exception | None = None):
        self._data = data
        self._raises = raises
        self.calls = 0

    def get_availability(self, **params):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._data


def test_preflight_propagates_definitive_mismatch():
    client = _FakeClient(data=SAMPLE)
    with pytest.raises(SessionError, match="not offered by provider 'verda'"):
        preflight_gpu_availability(client, "T4", "verda", None)


def test_preflight_passes_through_on_valid_combo():
    client = _FakeClient(data=SAMPLE)
    preflight_gpu_availability(client, "T4", "gcp", None)  # no raise
    assert client.calls == 1


def test_preflight_is_silent_when_availability_fetch_fails():
    """A flaky availability endpoint must NOT block a job submission."""
    client = _FakeClient(raises=RuntimeError("network down"))
    preflight_gpu_availability(client, "T4", "verda", None)  # swallowed, no raise


def test_preflight_is_silent_when_payload_has_no_models():
    client = _FakeClient(data={})  # malformed/empty → nothing to validate against
    preflight_gpu_availability(client, "T4", "verda", None)  # no raise
