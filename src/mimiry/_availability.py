"""Pre-create validation of the requested GPU/provider/location.

The scheduler rejects an impossible ``gpu``/``provider`` pairing only *after*
a session is created — e.g. asking for a ``T4`` from ``verda`` (which doesn't
carry it) fails the session with ``unsupported GPU type "T4"`` after a
provisioning round-trip. This module catches that locally, before the POST,
and turns it into an actionable error that names the providers which *do*
offer the GPU.

Design: the check is **best-effort**. A definitive mismatch (the GPU isn't
offered, or not by the requested provider/location) raises ``SessionError``.
But any failure to *consult* availability — network error, malformed payload,
empty list — is swallowed: a flaky availability endpoint must never block an
otherwise-valid job. The API stays the source of truth; this is just a fast,
friendly pre-flight.
"""

from __future__ import annotations

from typing import Any

from mimiry.exceptions import SessionError


def check_gpu_offered(
    models: list[dict],
    gpu: str,
    provider: str | None,
    location: str | None,
) -> None:
    """Raise ``SessionError`` if ``gpu`` (optionally pinned to ``provider`` /
    ``location``) isn't offered according to the availability ``models`` list.

    ``gpu`` matches either a model ``name`` (e.g. ``"H100_SXM"``) or ``family``
    (e.g. ``"H100"``). Returns ``None`` when the request is satisfiable.
    """
    matches = [m for m in models if m.get("name") == gpu or m.get("family") == gpu]
    if not matches:
        offered = sorted({m.get("name") for m in models if m.get("name")})
        raise SessionError(
            f"GPU type {gpu!r} is not offered. Available types: "
            f"{', '.join(offered) or 'none'}."
        )

    available = [m for m in matches if m.get("available")]
    if not available:
        raise SessionError(f"GPU type {gpu!r} is currently unavailable on all providers.")

    if provider is None:
        return  # available somewhere; let the scheduler pick a provider

    # Collapse the available matches into provider → set(locations).
    prov_locs: dict[str, set[str]] = {}
    for m in available:
        for p in m.get("providers", []):
            name = p.get("provider")
            if name:
                prov_locs.setdefault(name, set()).update(p.get("locations") or [])

    if provider not in prov_locs:
        offerers = ", ".join(sorted(prov_locs)) or "none"
        raise SessionError(
            f"GPU {gpu!r} is not offered by provider {provider!r}. "
            f"Available providers for {gpu}: {offerers}. "
            f"Pass provider=<one of those>, or omit the provider hint to let "
            f"Mimiry choose."
        )

    if location is not None and location not in prov_locs[provider]:
        locs = ", ".join(sorted(prov_locs[provider])) or "none"
        raise SessionError(
            f"Provider {provider!r} offers {gpu} but not in location {location!r}. "
            f"Available locations: {locs}."
        )


def preflight_gpu_availability(
    client: Any,
    gpu: str,
    provider: str | None,
    location: str | None = None,
) -> None:
    """Best-effort pre-create check. Raises ``SessionError`` on a definitive
    mismatch; silently returns if availability can't be consulted.

    ``client`` only needs a ``get_availability()`` method (see
    :class:`mimiry._client.MimiryClient`).
    """
    try:
        data = client.get_availability()
    except Exception:
        return  # never block submission on an availability-endpoint hiccup
    models = data.get("gpu_models") if isinstance(data, dict) else None
    if not models:
        return  # nothing to validate against — defer to the API
    check_gpu_offered(models, gpu, provider, location)
