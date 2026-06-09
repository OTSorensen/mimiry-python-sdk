"""Client-layer tests for volume + transaction methods: correct verb + path + body.

We stub ``MimiryClient._request`` so no real HTTP happens, and assert the
method/path/json the client would send (matching the mirc.sh endpoints).
"""

from __future__ import annotations

from typing import Any

import pytest

from mimiry._client import MimiryClient


class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


@pytest.fixture
def client(monkeypatch):
    c = MimiryClient.__new__(MimiryClient)  # bypass __init__ (no token/http needed)
    calls: list[dict] = []

    def fake_request(method: str, path: str, **kwargs: Any) -> _FakeResp:
        calls.append({"method": method, "path": path, **kwargs})
        # Return whatever the caller pre-seeded for this path, else empty 200.
        return c._next_resp

    c._request = fake_request  # type: ignore[attr-defined]
    c._calls = calls  # type: ignore[attr-defined]
    c._next_resp = _FakeResp(200, {})  # type: ignore[attr-defined]
    return c


def test_create_volume_posts(client):
    client._next_resp = _FakeResp(200, {"id": "v1", "state": "submitted"})
    out = client.create_volume({"name": "data", "size_gb": 100})
    call = client._calls[-1]
    assert call["method"] == "POST" and call["path"] == "/volumes"
    assert call["json"] == {"name": "data", "size_gb": 100}
    assert out["id"] == "v1"


def test_list_volumes_unwraps(client):
    client._next_resp = _FakeResp(200, {"volumes": [{"id": "v1"}, {"id": "v2"}]})
    out = client.list_volumes(state_not="deleted")
    call = client._calls[-1]
    assert call["method"] == "GET" and call["path"] == "/volumes"
    assert call["params"] == {"state_not": "deleted"}
    assert [v["id"] for v in out] == ["v1", "v2"]


def test_get_volume(client):
    client._next_resp = _FakeResp(200, {"id": "v9"})
    assert client.get_volume("v9")["id"] == "v9"
    call = client._calls[-1]
    assert call["method"] == "GET" and call["path"] == "/volumes/v9"


def test_extend_volume_patches_size(client):
    client._next_resp = _FakeResp(200, {"id": "v1", "size_gb": 200})
    client.extend_volume("v1", 200)
    call = client._calls[-1]
    assert call["method"] == "PATCH" and call["path"] == "/volumes/v1"
    assert call["json"] == {"size_gb": 200}


def test_delete_volume_202_returns_none(client):
    client._next_resp = _FakeResp(202, {})
    assert client.delete_volume("v1") is None
    call = client._calls[-1]
    assert call["method"] == "DELETE" and call["path"] == "/volumes/v1"


def test_get_transactions(client):
    client._next_resp = _FakeResp(200, {"transactions": []})
    client.get_transactions(limit=10)
    call = client._calls[-1]
    assert call["method"] == "GET" and call["path"] == "/transactions"
    assert call["params"] == {"limit": 10}
