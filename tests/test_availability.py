"""Tests for pre-create GPU validation + family-alias resolution against /availability."""

from __future__ import annotations

import pytest

from mimiry._availability import check_gpu_offered, preflight_gpu_availability
from mimiry.exceptions import SessionError

# Mirrors the shape of GET /api/compute/v1/availability (abbreviated). Note the
# catalog exposes concrete names (e.g. "T4_16G_PCIe") with a "family" alias.
SAMPLE = {
    "gpu_models": [
        {
            "name": "T4_16G_PCIe", "family": "T4", "available": True, "currency": "EUR",
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
            "name": "A100_40G_SXM", "family": "A100", "available": False, "currency": "EUR",
            "providers": [{"provider": "verda", "hourly_rate": 1.2, "locations": ["FIN-01"]}],
        },
        # Two available V100 variants → the family alias "V100" is ambiguous.
        {
            "name": "V100_16G", "family": "V100", "available": True, "currency": "EUR",
            "providers": [{"provider": "verda", "hourly_rate": 0.8, "locations": ["FIN-01"]}],
        },
        {
            "name": "V100_32G", "family": "V100", "available": True, "currency": "EUR",
            "providers": [{"provider": "verda", "hourly_rate": 0.9, "locations": ["FIN-01"]}],
        },
    ]
}
MODELS = SAMPLE["gpu_models"]


# ────────────────────────── family-alias resolution ──────────────────────────


def test_resolves_family_alias_to_concrete_name():
    assert check_gpu_offered(MODELS, "T4", "gcp", None) == "T4_16G_PCIe"


def test_resolves_family_alias_without_provider_hint():
    assert check_gpu_offered(MODELS, "T4", None, None) == "T4_16G_PCIe"


def test_concrete_name_passes_through():
    assert check_gpu_offered(MODELS, "T4_16G_PCIe", "gcp", None) == "T4_16G_PCIe"


def test_family_resolves_under_each_provider():
    # "H100" is a family; the offered model name is "H100_SXM".
    assert check_gpu_offered(MODELS, "H100", "acme", None) == "H100_SXM"
    assert check_gpu_offered(MODELS, "H100_SXM", "verda", None) == "H100_SXM"


def test_ambiguous_family_raises_with_options():
    with pytest.raises(SessionError, match="multiple available types") as exc:
        check_gpu_offered(MODELS, "V100", None, None)
    assert "V100_16G" in str(exc.value) and "V100_32G" in str(exc.value)


# ────────────────────────── validation errors (unchanged behaviour) ──────────────────────────


def test_raises_when_provider_does_not_offer_gpu():
    with pytest.raises(SessionError, match="not offered by provider 'verda'") as exc:
        check_gpu_offered(MODELS, "T4", "verda", None)
    assert "gcp" in str(exc.value)  # points at the provider that *does* offer it


def test_raises_when_gpu_type_unknown():
    with pytest.raises(SessionError, match="not offered"):
        check_gpu_offered(MODELS, "B200", None, None)


def test_raises_when_gpu_unavailable_everywhere():
    with pytest.raises(SessionError, match="currently unavailable"):
        check_gpu_offered(MODELS, "A100", None, None)


def test_ok_when_location_matches():
    assert check_gpu_offered(MODELS, "T4", "gcp", "europe-west4-a") == "T4_16G_PCIe"


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


def test_preflight_returns_resolved_name_on_valid_combo():
    client = _FakeClient(data=SAMPLE)
    assert preflight_gpu_availability(client, "T4", "gcp", None) == "T4_16G_PCIe"
    assert client.calls == 1


def test_preflight_returns_gpu_unchanged_when_fetch_fails():
    """A flaky availability endpoint must NOT block a job submission."""
    client = _FakeClient(raises=RuntimeError("network down"))
    assert preflight_gpu_availability(client, "T4", "verda", None) == "T4"


def test_preflight_returns_gpu_unchanged_when_no_models():
    client = _FakeClient(data={})  # malformed/empty → nothing to validate against
    assert preflight_gpu_availability(client, "T4", "verda", None) == "T4"
