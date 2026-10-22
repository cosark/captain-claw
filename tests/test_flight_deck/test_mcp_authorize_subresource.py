"""The MCP OAuth ``/authorize`` page sends on only a page, never a subresource.

Anyone may register a client (RFC 7591), with any https redirect URI. If
``/authorize`` redirected an ``<img>`` there, markdown somebody else wrote
(``![](/authorize?client_id=…&redirect_uri=https://evil/…)``) would make the
reader's browser tell that host who looked, and when — the relative URL gets
past the filter that drops remote images. An image, script or style request
gets an error page instead; a sign-in in a browser window is unchanged.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import mcp_oauth_routes as R


class _FakeDB:
    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}

    async def prune_oauth(self) -> None:
        pass

    async def create_oauth_client(self, cid: str, name: str, uris: list[str]) -> None:
        self.clients[cid] = {"client_name": name, "redirect_uris": uris}

    async def get_oauth_client(self, cid: str) -> dict | None:
        return self.clients.get(cid)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = _FakeDB()
    monkeypatch.setattr(R, "get_db", lambda: db)
    app = FastAPI()
    app.include_router(R.router)
    return TestClient(app)


def _authorize_url(c: TestClient) -> str:
    r = c.post("/register", json={"redirect_uris": ["https://evil.example/p"]})
    assert r.status_code in (200, 201)
    cid = r.json()["client_id"]
    return f"/authorize?client_id={cid}&redirect_uri=https://evil.example/p&response_type=x&state=notes42"


@pytest.mark.parametrize("dest", ["image", "script", "style", "font", "empty"])
def test_a_subresource_gets_no_redirect(client: TestClient, dest: str):
    r = client.get(_authorize_url(client), headers={"Sec-Fetch-Dest": dest, "Sec-Fetch-Site": "same-origin"},
                   follow_redirects=False)
    assert r.status_code == 400
    assert "location" not in r.headers


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Dest": "document"}, {}])
def test_a_page_is_sent_on_as_before(client: TestClient, headers: dict):
    r = client.get(_authorize_url(client), headers=headers, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://evil.example/p?error=unsupported_response_type")
