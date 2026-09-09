"""Pre-create validation of the requested GPU/provider/location.

The scheduler rejects an impossible ``gpu``/``provider`` pairing only *after*
a session is created — e.g. asking for a ``T4`` from ``verda`` (which doesn't
carry it) fails the session with ``unsupported GPU type "T4"`` after a
provisioning round-trip. This module catches that locally, before the POST,
and turns it into an actionable error that names the providers which *do*
offer the GPU. It also resolves a GPU *family* alias (``"A100"``) to the
concrete catalog names the API requires (``"A100_40G_SXM"``, ...), cheapest
first — ``gpu.types`` is a preference list, so a family that maps to several
types is sent as all of them rather than refused.

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
) -> list[str]:
    """Resolve ``gpu`` to the concrete catalog names the API expects, validating
    the optional ``provider`` / ``location`` hints against ``models``.

    ``gpu`` may be a concrete model ``name`` (e.g. ``"A100_40G_SXM"``) or a
    ``family`` alias (e.g. ``"A100"``). The result is the ``gpu.types``
    preference list: every available type that satisfies the hints, cheapest
    hourly rate first. Raises ``SessionError`` if the type isn't offered, is
    unavailable, or can't be satisfied by the requested provider/location.
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
        if location is not None:
            # No provider hint, but a location — which is binding when it came
            # from a mounted volume. Refuse now rather than after the platform
            # reports "no GPU matches criteria" on a session that already exists.
            offered_at = _by_price(gpu, available, None, location)
            if offered_at == [gpu]:
                locs = sorted({
                    loc for m in available for p in m.get("providers", [])
                    for loc in (p.get("locations") or [])
                })
                raise SessionError(
                    f"GPU {gpu!r} is not currently offered in location {location!r}. "
                    f"Available locations: {', '.join(locs) or 'none'}."
                )
            return offered_at
        return _by_price(gpu, available, None, None)

    # Collapse available matches into provider → set(locations) so a mismatch
    # can name the providers and locations that do offer the GPU.
    prov_locs: dict[str, set[str]] = {}
    for m in available:
        for p in m.get("providers", []):
            pname = p.get("provider")
            if not pname:
                continue
            prov_locs.setdefault(pname, set()).update(p.get("locations") or [])

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

    return _by_price(gpu, available, provider, location)


def _by_price(
    gpu: str, models: list[dict], provider: str | None, location: str | None
) -> list[str]:
    """Order the concrete names in ``models`` that satisfy the hints by their
    cheapest matching hourly rate. Falls back to ``[gpu]`` when nothing in the
    catalog carries a name, so the API still gets to decide.
    """
    priced: list[tuple[float, str]] = []
    for m in models:
        name = m.get("name")
        if not name:
            continue
        rates = [
            float(p.get("hourly_rate") or 0)
            for p in m.get("providers", [])
            if (provider is None or p.get("provider") == provider)
            and (location is None or location in (p.get("locations") or []))
        ]
        if rates:
            priced.append((min(rates), name))
    if not priced:
        return [gpu]
    return [name for _, name in sorted(priced)]


def preflight_gpu_availability(
    client: Any,
    gpu: str,
    provider: str | None,
    location: str | None = None,
) -> list[str]:
    """Best-effort pre-create check that also resolves a GPU family alias to the
    concrete catalog names the API expects.

    Returns the list to send in ``gpu.types`` — the resolved concrete names,
    cheapest first, or ``[gpu]`` unchanged if availability can't be consulted.
    Raises ``SessionError`` on a definitive mismatch. ``client`` only needs a ``get_availability()``
    method (see :class:`mimiry._client.MimiryClient`).
    """
    try:
        data = client.get_availability()
    except Exception:
        return [gpu]  # never block submission on an availability-endpoint hiccup
    models = data.get("gpu_models") if isinstance(data, dict) else None
    if not models:
        return [gpu]  # nothing to validate against — defer to the API
    return check_gpu_offered(models, gpu, provider, location)
