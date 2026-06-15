"""Pre-create validation of the requested GPU/provider/location.

The scheduler rejects an impossible ``gpu``/``provider`` pairing only *after*
a session is created — e.g. asking for a ``T4`` from ``verda`` (which doesn't
carry it) fails the session with ``unsupported GPU type "T4"`` after a
provisioning round-trip. This module catches that locally, before the POST,
and turns it into an actionable error that names the providers which *do*
offer the GPU. It also resolves a GPU *family* alias (``"T4"``) to the concrete
catalog name the API now requires (``"T4_16G_PCIe"``).

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
) -> str:
    """Resolve ``gpu`` to the concrete catalog name the API expects, validating
    the optional ``provider`` / ``location`` hints against ``models``.

    ``gpu`` may be a concrete model ``name`` (e.g. ``"T4_16G_PCIe"``) or a
    ``family`` alias (e.g. ``"T4"``); a family that maps to exactly one
    available type is resolved to that type's name. Raises ``SessionError`` if
    the type isn't offered, is unavailable, can't be satisfied by the requested
    provider/location, or a family alias is ambiguous (maps to several
    available types).
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
        return _resolve_one(gpu, {m.get("name") for m in available if m.get("name")})

    # Collapse available matches into provider → set(locations), and collect the
    # concrete catalog names that satisfy the provider (and location) hint.
    prov_locs: dict[str, set[str]] = {}
    candidates: set[str] = set()
    for m in available:
        satisfies = False
        for p in m.get("providers", []):
            pname = p.get("provider")
            if not pname:
                continue
            locs = p.get("locations") or []
            prov_locs.setdefault(pname, set()).update(locs)
            if pname == provider and (location is None or location in locs):
                satisfies = True
        if satisfies and m.get("name"):
            candidates.add(m["name"])

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

    return _resolve_one(gpu, candidates)


def _resolve_one(gpu: str, names: set[str | None]) -> str:
    """Pick the single concrete catalog name from ``names``.

    Returns it when there's exactly one; returns ``gpu`` unchanged when there's
    nothing to resolve to (defer to the API); raises ``SessionError`` when a
    family alias is ambiguous so the caller can pick an exact type.
    """
    resolved = {n for n in names if n}
    if len(resolved) == 1:
        return next(iter(resolved))
    if not resolved:
        return gpu
    raise SessionError(
        f"GPU {gpu!r} matches multiple available types: "
        f"{', '.join(sorted(resolved))}. Pass one of those exact names."
    )


def preflight_gpu_availability(
    client: Any,
    gpu: str,
    provider: str | None,
    location: str | None = None,
) -> str:
    """Best-effort pre-create check that also resolves a GPU family alias to the
    concrete catalog name the API expects.

    Returns the name to send in ``gpu.types`` — the resolved concrete name, or
    ``gpu`` unchanged if availability can't be consulted. Raises ``SessionError``
    on a definitive mismatch. ``client`` only needs a ``get_availability()``
    method (see :class:`mimiry._client.MimiryClient`).
    """
    try:
        data = client.get_availability()
    except Exception:
        return gpu  # never block submission on an availability-endpoint hiccup
    models = data.get("gpu_models") if isinstance(data, dict) else None
    if not models:
        return gpu  # nothing to validate against — defer to the API
    return check_gpu_offered(models, gpu, provider, location)
