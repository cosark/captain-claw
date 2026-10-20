"""An MCP server is private to the user who added it: only that user's agents
use it, and only that user sees, calls or manages it — unless an admin made it
deck-wide. Pre-ownership OAuth servers go to whoever signed them in.

Users and agents are picked per request (``X-Test-User`` / ``X-Test-Agent``);
upstream MCP traffic goes to a fake via a patched ``httpx.AsyncClient``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from captain_claw.flight_deck import app_routes, mcp_client_oauth, mcp_manager, mcp_routes, mcp_storage
from captain_claw.flight_deck.agent_identity import AgentCaller
from captain_claw.flight_deck.auth import get_current_user

USERS = {
    "alice": {"id": "alice-id", "email": "alice@example.com", "role": "user"},
    "bob": {"id": "bob-id", "email": "bob@example.com", "role": "user"},
    "admin": {"id": "admin-id", "email": "admin@example.com", "role": "admin"},
}
URL = "https://upstream.example/mcp"


def _upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content) if request.content else {}
    method, rid = body.get("method"), body.get("id")
    if method == "initialize":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-03-26"}})
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "tools/list":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "lookup"}]}})
    if method == "tools/call":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": "secret portfolio data"}]}})
    return httpx.Response(400)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("CAPTAIN_CLAW_FD_MCP_PATH", str(tmp_path / "mcp_servers.json"))
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(_upstream)}))
    mcp_manager._manager = None  # noqa: SLF001
    yield
    mcp_manager._manager = None  # noqa: SLF001


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def _caller(request: Request, *, what: str) -> AgentCaller:
        slug, _, owner = request.headers.get("X-Test-Agent", "").partition(":")
        return AgentCaller(slug=slug, owner=owner)

    monkeypatch.setattr(mcp_routes, "require_agent_caller", _caller)
    app = FastAPI()
    app.include_router(mcp_routes.router)
    app.include_router(app_routes.router)
    def _user(request: Request) -> dict[str, Any]:
        return USERS[request.headers.get("X-Test-User", "alice")]

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


def _as(user: str) -> dict[str, str]:
    return {"X-Test-User": user}


def _agent(owner: str, slug: str = "helper") -> dict[str, str]:
    return {"X-Test-Agent": f"{slug}:{USERS[owner]['id'] if owner in USERS else owner}"}


def _names(client: TestClient, user: str) -> list[str]:
    return [s["name"] for s in client.get("/fd/mcp/servers", headers=_as(user)).json()["servers"]]


def _add(client: TestClient, user: str, name: str = "fric", **extra: Any) -> httpx.Response:
    return client.post("/fd/mcp/servers", headers=_as(user), json={"name": name, "url": URL, **extra})


# ── a private server ────────────────────────────────────────────────


def test_a_new_server_belongs_to_its_creator_and_no_one_else_sees_it(client: TestClient) -> None:
    resp = _add(client, "alice")
    assert resp.status_code == 200
    assert mcp_storage.get_server("fric")["owner_id"] == "alice-id"
    assert mcp_storage.get_server("fric")["deck_wide"] is False

    mine = client.get("/fd/mcp/servers", headers=_as("alice")).json()["servers"]
    assert mine[0]["can_use"] is True and mine[0]["can_manage"] is True
    assert _names(client, "bob") == []
    assert _names(client, "admin") == []  # admins don't see users' private servers either


def test_other_users_cant_reach_a_private_server_in_any_way(client: TestClient) -> None:
    _add(client, "alice", client_secret="alice-secret", client_id="cid", token_endpoint="/token")
    bob = _as("bob")
    assert client.post("/fd/mcp/servers/fric/test", headers=bob).status_code == 404
    assert client.delete("/fd/mcp/servers/fric", headers=bob).status_code == 404
    assert client.get("/fd/mcp/fric/user_tools", headers=bob).status_code == 404
    assert client.post("/fd/mcp/fric/user_call", headers=bob, json={"tool": "lookup"}).status_code == 404
    # Re-saving under the same name is refused without saying whose it is.
    resp = _add(client, "bob", url="https://evil.example/mcp")
    assert resp.status_code == 409
    assert mcp_storage.get_server("fric")["url"] == URL
    # A probe under that name doesn't borrow Alice's stored secret.
    resp = client.post("/fd/mcp/probe", headers=bob, json={"name": "fric", "url": "https://evil.example/mcp"})
    assert resp.status_code == 409
    assert mcp_storage.get_server("fric")["owner_id"] == "alice-id"
    # The admin is "someone else" too.
    assert client.post("/fd/mcp/fric/user_call", headers=_as("admin"), json={"tool": "lookup"}).status_code == 404


def test_only_the_owners_agents_get_the_tools(client: TestClient) -> None:
    _add(client, "alice")
    alice_agent, bob_agent = _agent("alice"), _agent("bob")

    assert client.get("/fd/mcp/agent/servers", headers=alice_agent).json() == {"servers": [{"name": "fric"}]}
    assert client.get("/fd/mcp/fric/tools", headers=alice_agent).json()["tools"] == [{"name": "lookup"}]
    resp = client.post("/fd/mcp/fric/call", headers=alice_agent, json={"tool": "lookup"})
    assert resp.json()["result"]["content"][0]["text"] == "secret portfolio data"

    assert client.get("/fd/mcp/agent/servers", headers=bob_agent).json() == {"servers": []}
    assert client.get("/fd/mcp/fric/tools", headers=bob_agent).status_code == 404
    assert client.post("/fd/mcp/fric/call", headers=bob_agent, json={"tool": "lookup"}).status_code == 404
    assert client.post("/fd/mcp/fric/call_stream", headers=bob_agent, json={"tool": "lookup"}).status_code == 404
    # An agent FD knows no owner for gets nothing private.
    assert client.get("/fd/mcp/agent/servers", headers=_agent("")).json() == {"servers": []}


def test_the_allowlist_still_narrows_the_owners_agents(client: TestClient) -> None:
    _add(client, "alice", allowed_agents=["analyst"])
    assert client.get("/fd/mcp/agent/servers", headers=_agent("alice", "analyst")).json()["servers"]
    assert client.get("/fd/mcp/agent/servers", headers=_agent("alice", "other")).json()["servers"] == []
    # Naming Bob's agent in the allowlist doesn't let Bob's agent in.
    _add(client, "alice", allowed_agents=["bobs-agent"])
    assert client.get("/fd/mcp/agent/servers", headers=_agent("bob", "bobs-agent")).json()["servers"] == []


def test_app_authoring_wont_list_someone_elses_tools(client: TestClient) -> None:
    _add(client, "alice")
    resp = client.post("/fd/apps/generate", headers=_as("bob"), json={"description": "x", "mcp_server": "fric"})
    assert resp.status_code == 404


# ── deck-wide ───────────────────────────────────────────────────────


def test_only_an_admin_makes_a_server_deck_wide(client: TestClient) -> None:
    assert _add(client, "alice", deck_wide=True).status_code == 403
    _add(client, "alice")
    assert _add(client, "alice", deck_wide=True).status_code == 403
    assert mcp_storage.get_server("fric")["deck_wide"] is False


def test_a_deck_wide_server_serves_everyone_and_only_admins_change_it(client: TestClient) -> None:
    assert _add(client, "admin", "shared", deck_wide=True).status_code == 200
    view = {s["name"]: s for s in client.get("/fd/mcp/servers", headers=_as("bob")).json()["servers"]}
    assert view["shared"]["can_use"] is True and view["shared"]["can_manage"] is False
    assert client.get("/fd/mcp/agent/servers", headers=_agent("bob")).json() == {"servers": [{"name": "shared"}]}
    assert client.post("/fd/mcp/shared/user_call", headers=_as("bob"), json={"tool": "lookup"}).status_code == 200

    assert _add(client, "bob", "shared", url="https://evil.example/mcp").status_code == 403
    assert client.delete("/fd/mcp/servers/shared", headers=_as("bob")).status_code == 403
    assert client.post("/fd/mcp/oauth/disconnect", headers=_as("bob"), json={"server": "shared"}).status_code == 403

    # The admin takes it back: private to its owner (the admin) again.
    assert _add(client, "admin", "shared", deck_wide=False).status_code == 200
    assert _names(client, "bob") == []
    assert client.get("/fd/mcp/agent/servers", headers=_agent("bob")).json() == {"servers": []}


def test_an_omitted_deck_wide_keeps_the_servers_setting(client: TestClient) -> None:
    _add(client, "admin", "shared", deck_wide=True)
    _add(client, "admin", "shared", enabled=False)  # no deck_wide in the payload
    assert mcp_storage.get_server("shared")["deck_wide"] is True


def test_admin_integrations_shares_and_unshares_without_touching_the_rest(client: TestClient) -> None:
    _add(client, "admin", "mine", client_id="cid", client_secret="s3cret", token_endpoint="/token",
         allowed_agents=["analyst"])
    resp = client.post("/fd/mcp/servers/mine/sharing", headers=_as("admin"), json={"deck_wide": True})
    assert resp.status_code == 200
    rec = mcp_storage.get_server("mine")
    assert rec["deck_wide"] is True and rec["owner_id"] == "admin-id"
    assert (rec["url"], rec["client_secret"], rec["allowed_agents"]) == (URL, "s3cret", ["analyst"])
    assert _names(client, "bob") == ["mine"]

    assert client.post("/fd/mcp/servers/mine/sharing", headers=_as("admin"),
                       json={"deck_wide": False}).status_code == 200
    assert _names(client, "bob") == []


def test_only_an_admin_shares_and_only_what_they_can_see(client: TestClient) -> None:
    _add(client, "alice")
    # The owner isn't an admin; the admin can't see Alice's private server.
    assert client.post("/fd/mcp/servers/fric/sharing", headers=_as("alice"),
                       json={"deck_wide": True}).status_code == 403
    assert client.post("/fd/mcp/servers/fric/sharing", headers=_as("admin"),
                       json={"deck_wide": True}).status_code == 404
    assert mcp_storage.get_server("fric")["deck_wide"] is False


def test_an_oauth_sign_in_is_only_for_the_servers_manager(client: TestClient) -> None:
    _add(client, "alice", auth="oauth")
    assert client.post("/fd/mcp/oauth/connect-ticket", headers=_as("bob"), json={"server": "fric"}).status_code == 404
    assert client.post("/fd/mcp/oauth/connect-ticket", headers=_as("alice"), json={"server": "fric"}).status_code == 200


# ── pre-ownership records ───────────────────────────────────────────


def _write_legacy(records: list[dict[str, Any]]) -> None:
    mcp_storage._storage_path().write_text(json.dumps(records))  # noqa: SLF001


def _write_grant(name: str, url: str, connected_by: str) -> None:
    mcp_client_oauth._write_all({name: {  # noqa: SLF001
        "server_url": url, "access_token": "at", "connected_by": connected_by}})


class _FakeDB:
    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return next((u for u in USERS.values() if u["email"] == email.lower()), None)


@pytest.mark.asyncio
async def test_legacy_oauth_server_adopts_whoever_signed_it_in() -> None:
    _write_legacy([
        {"name": "fric", "url": URL, "auth": "oauth"},
        {"name": "gone", "url": "https://gone.example/mcp", "auth": "oauth"},
        {"name": "plain", "url": "https://plain.example/mcp", "headers": {"X-Key": "k"}},
    ])
    _write_grant("fric", URL, "Alice@Example.com")
    grants = mcp_client_oauth._load_all()  # noqa: SLF001
    grants["gone"] = {"server_url": "https://gone.example/mcp", "access_token": "at", "connected_by": "left@example.com"}
    mcp_client_oauth._write_all(grants)  # noqa: SLF001

    # Before the migration runs, a legacy sign-in already fails closed.
    assert mcp_storage.get_server("fric")["deck_wide"] is False
    assert not mcp_storage.agent_may_use(mcp_storage.get_server("fric"), "helper", "bob-id")

    await mcp_routes.migrate_legacy_ownership(_FakeDB())
    fric, gone, plain = (mcp_storage.get_server(n) for n in ("fric", "gone", "plain"))
    assert (fric["owner_id"], fric["deck_wide"]) == ("alice-id", False)
    assert (gone["owner_id"], gone["deck_wide"]) == ("", False)
    assert (plain["owner_id"], plain["deck_wide"]) == ("", True)  # stays as it behaved
    assert mcp_storage.agent_may_use(fric, "helper", "alice-id")
    assert not mcp_storage.agent_may_use(fric, "helper", "bob-id")
    assert not mcp_storage.agent_may_use(gone, "helper", "alice-id")

    # Idempotent: a second run changes nothing.
    assert await mcp_storage.migrate_legacy_ownership(lambda _r: None) == []


def test_an_ownerless_server_is_the_admins_to_remove_but_nobodys_to_use(client: TestClient) -> None:
    _write_legacy([{"name": "gone", "url": URL, "auth": "oauth", "owner_id": "", "deck_wide": False}])
    view = client.get("/fd/mcp/servers", headers=_as("admin")).json()["servers"]
    assert [(s["name"], s["can_use"], s["can_manage"]) for s in view] == [("gone", False, True)]
    assert _names(client, "alice") == []
    assert client.post("/fd/mcp/servers/gone/test", headers=_as("admin")).status_code == 403
    assert client.post("/fd/mcp/gone/user_call", headers=_as("admin"), json={"tool": "x"}).status_code == 403
    assert client.delete("/fd/mcp/servers/gone", headers=_as("admin")).status_code == 200


# ── auth off ────────────────────────────────────────────────────────


def test_with_auth_off_ownership_is_not_enforced(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _add(client, "alice")
    monkeypatch.setenv("FD_AUTH_ENABLED", "false")
    assert client.get("/fd/mcp/agent/servers", headers=_agent("")).json() == {"servers": [{"name": "fric"}]}
    assert _names(client, "bob") == ["fric"]
