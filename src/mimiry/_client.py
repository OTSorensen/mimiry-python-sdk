"""Thin HTTP wrapper over the Mimiry compute API.

Stateless except for the Token. Higher-level lifecycle helpers (polling for
state transitions, scanning logs for sentinels) live in ``_session.py``.
"""

from __future__ import annotations

from typing import Any

import httpx

from mimiry._auth import Token
from mimiry.exceptions import SessionError


class MimiryClient:
    """Thin client for /api/v1/* and /api/compute/v1/*."""

    def __init__(self, token: Token, http_timeout: float = 30.0) -> None:
        self._token = token
        self._compute_base = f"{token.api_base}/api/compute/v1"
        self._http = httpx.Client(timeout=http_timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MimiryClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def token(self) -> Token:
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token.get()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._compute_base}{path}"
        try:
            resp = self._http.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as e:
            raise SessionError(f"{method} {path} request failed: {e}") from e
        return resp

    @staticmethod
    def _json_or_raise(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:500]
            raise SessionError(f"HTTP {resp.status_code} on {resp.request.url}: {body}")
        return resp.json()

    # ────────── balance / quota / availability ──────────

    def get_balance(self) -> dict:
        return self._json_or_raise(self._request("GET", "/balance"))

    def get_quota(self) -> dict:
        return self._json_or_raise(self._request("GET", "/quota"))

    def get_availability(self, **params: Any) -> dict:
        """Public endpoint — no auth required, but it's convenient to call from the client."""
        return self._json_or_raise(self._http.get(f"{self._compute_base}/availability", params=params))

    # ────────── sessions ──────────

    def create_session(self, payload: dict) -> dict:
        """POST /sessions. Returns the initial session object (state=submitted)."""
        return self._json_or_raise(self._request("POST", "/sessions", json=payload))

    def get_session(self, session_id: str, *, events_tail: int | None = None) -> dict:
        params = {}
        if events_tail is not None:
            params["events_tail"] = events_tail
        return self._json_or_raise(self._request("GET", f"/sessions/{session_id}", params=params))

    def list_sessions(self, **params: Any) -> list[dict]:
        body = self._json_or_raise(self._request("GET", "/sessions", params=params))
        return body.get("sessions", body) if isinstance(body, dict) else body

    def terminate_session(self, session_id: str) -> dict | None:
        """DELETE /sessions/{id}. Returns the response body or None on 202/204."""
        resp = self._request("DELETE", f"/sessions/{session_id}")
        if resp.status_code in (202, 204):
            return None
        return self._json_or_raise(resp)

    def get_logs(self, session_id: str, *, tail: int = 200, timestamps: bool = False) -> dict:
        """GET /sessions/{id}/logs.

        Returns a dict like ``{"logs": "<text>"}`` on 200, or
        ``{"retry_after_seconds": N}`` on 503 (container still booting).

        Caller is responsible for retry/backoff loops — see :func:`mimiry._session.wait_for_marker`.
        """
        resp = self._request(
            "GET",
            f"/sessions/{session_id}/logs",
            params={"tail": tail, "timestamps": "true" if timestamps else "false"},
        )
        if resp.status_code == 503:
            try:
                body = resp.json()
            except Exception:
                body = {"retry_after_seconds": 5}
            return {"_status": 503, **body}
        if resp.status_code == 409:
            try:
                body = resp.json()
            except Exception:
                body = {"message": resp.text}
            return {"_status": 409, **body}
        return {"_status": 200, **self._json_or_raise(resp)}
