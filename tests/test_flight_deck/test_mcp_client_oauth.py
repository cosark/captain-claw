"""OAuth sign-in to a remote MCP server — discovery, Dynamic Client
Registration, PKCE, refresh — against a fake MCP server + authorization
server living in one :class:`httpx.MockTransport`.

The browser leg (the user approving at the authorization endpoint) is played
by :meth:`FakeDCRServer.approve`, which turns the authorization URL Flight
Deck built into the code the server would redirect back with.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import mcp_client_oauth, mcp_manager, mcp_routes, mcp_storage
from captain_claw.flight_deck.auth import get_current_user

MCP_URL = "https://mcp.example/mcp"
ISSUER = "https://auth.example"
USER = {"id": "u-1", "email": "pilot@example.com", "role": "user"}


class FakeDCRServer:
    """An MCP server that wants a bearer token, and the authorization server
    that hands them out (RFC 9728 + 8414 + 7591 + PKCE + rotating refresh)."""

    def __init__(self, *, prm: bool = True, as_metadata: bool = True) -> None:
        self.prm = prm
        self.as_metadata = as_metadata
        self.registrations: list[dict[str, Any]] = []
        self.codes: dict[str, dict[str, Any]] = {}
        self.valid_access: set[str] = set()
        self.valid_refresh: set[str] = set()
        self.token_requests: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self.mcp_auth_headers: list[str] = []
        self.counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # The user approves in the browser: authorization URL → redirect code.
    def approve(self, authorization_url: str) -> tuple[str, dict[str, str]]:
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        self.counter += 1
        code = f"code-{self.counter}"
        self.codes[code] = q
        return code, q

    def _issue(self) -> dict[str, Any]:
        self.counter += 1
        access, refresh = f"at-{self.counter}", f"rt-{self.counter}"
        self.valid_access.add(access)
        self.valid_refresh.add(refresh)
        return {"access_token": access, "refresh_token": refresh,
                "token_type": "Bearer", "expires_in": 3600, "scope": "read"}

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host, path = url.host, url.path
        if host == "mcp.example" and path == "/mcp":
            return self._mcp(request)
        if host == "mcp.example" and path == "/.well-known/oauth-protected-resource/mcp" and self.prm:
            return httpx.Response(200, json={
                "resource": MCP_URL, "authorization_servers": [ISSUER], "scopes_supported": ["read"],
            })
        if host == "auth.example" and path == "/.well-known/oauth-authorization-server" and self.as_metadata:
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "registration_endpoint": f"{ISSUER}/register",
                "revocation_endpoint": f"{ISSUER}/revoke",
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if path == "/register" and request.method == "POST":
            body = json.loads(request.content)
            self.registrations.append(body)
            return httpx.Response(201, json={
                "client_id": f"dyn-{len(self.registrations)}",
                "redirect_uris": body["redirect_uris"],
                "token_endpoint_auth_method": "none",
            })
        if path == "/token" and request.method == "POST":
            return self._token({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        if path == "/revoke" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.revoked.append(form.get("token", ""))
            self.valid_refresh.discard(form.get("token", ""))
            return httpx.Response(200)
        return httpx.Response(404, json={"error": "not_found"})

    def _token(self, form: dict[str, str]) -> httpx.Response:
        self.token_requests.append(form)
        if form.get("grant_type") == "authorization_code":
            q = self.codes.pop(form.get("code", ""), None)
            if not q:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad code"})
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if challenge != q["code_challenge"] or form.get("redirect_uri") != q["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
            if form.get("client_id") != q["client_id"] or form.get("resource") != MCP_URL:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "client/resource"})
            return httpx.Response(200, json=self._issue())
        if form.get("grant_type") == "refresh_token":
            rt = form.get("refresh_token", "")
            if rt not in self.valid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "used"})
            self.valid_refresh.discard(rt)  # rotate: single use
            return httpx.Response(200, json=self._issue())
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _mcp(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        self.mcp_auth_headers.append(auth)
        if auth.removeprefix("Bearer ") not in self.valid_access:
            challenge = 'Bearer error="invalid_token", scope="read"'
            if self.prm:
                challenge += ', resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/mcp"'
            return httpx.Response(401, json={"error": "unauthorized"},
                                  headers={"WWW-Authenticate": challenge})
        body = json.loads(request.content)
        method, rid = body.get("method"), body.get("id")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-03-26", "serverInfo": {"name": "fake", "version": "0"}}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {
                "tools": [{"name": "search", "inputSchema": {"type": "object"}}]}})
        if method == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "found it"}]}})
        return httpx.Response(400)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("CAPTAIN_CLAW_FD_MCP_PATH", str(tmp_path / "mcp_servers.json"))
    monkeypatch.delenv("FD_PUBLIC_URL", raising=False)
    mcp_manager._manager = None  # noqa: SLF001
    mcp_client_oauth._refresh_locks.clear()  # noqa: SLF001
    yield
    mcp_manager._manager = None  # noqa: SLF001


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDCRServer:
    server = FakeDCRServer()
    real = httpx.AsyncClient

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = server.transport()
        return real(*args, **kwargs)

    # Both the OAuth module and the MCP transport build their clients here.
    monkeypatch.setattr(httpx, "AsyncClient", _client)
    return server


async def _save_oauth_server(**extra: Any) -> dict[str, Any]:
    return await mcp_storage.upsert_server({
        "name": "remote", "url": MCP_URL, "auth": "oauth", "owner_id": USER["id"], **extra,
    })


async def _sign_in(fake: FakeDCRServer, record: dict[str, Any]) -> dict[str, Any]:
    url, pending = await mcp_client_oauth.begin_sign_in(record, "https://deck.example/fd/mcp/oauth/callback", "st-1")
    code, _ = fake.approve(url)
    return await mcp_client_oauth.complete_sign_in(pending, code, connected_by="pilot@example.com")


# ── pieces ──────────────────────────────────────────────────────────


def test_parse_www_authenticate() -> None:
    parse = mcp_client_oauth.parse_www_authenticate
    assert parse('Bearer resource_metadata="https://x/.well-known/p", scope="a b"') == {
        "resource_metadata": "https://x/.well-known/p", "scope": "a b"}
    assert parse('Basic realm="x"') == {}
    assert parse("Bearer") == {}
    assert parse('Bearer error=invalid_token')["error"] == "invalid_token"


def test_canonical_resource_and_covers() -> None:
    assert mcp_client_oauth.canonical_resource("HTTPS://MCP.Example/mcp/#x") == "https://mcp.example/mcp"
    assert mcp_client_oauth._covers("https://mcp.example", MCP_URL)  # noqa: SLF001
    assert mcp_client_oauth._covers("https://mcp.example/mcp/", MCP_URL)  # noqa: SLF001
    assert not mcp_client_oauth._covers("https://mcp.example/other", MCP_URL)  # noqa: SLF001
    assert not mcp_client_oauth._covers("https://evil.example/mcp", MCP_URL)  # noqa: SLF001


@pytest.mark.asyncio
async def test_discover_follows_the_401_challenge(fake: FakeDCRServer) -> None:
    found = await mcp_client_oauth.discover(MCP_URL)
    assert found["issuer"] == ISSUER
    assert found["authorization_endpoint"] == f"{ISSUER}/authorize"
    assert found["registration_endpoint"] == f"{ISSUER}/register"
    assert found["resource"] == MCP_URL
    assert found["scope"] == "read"
    assert found["fallback"] is False


@pytest.mark.asyncio
async def test_discover_falls_back_to_the_servers_origin(fake: FakeDCRServer) -> None:
    fake.prm = False
    fake.as_metadata = False
    found = await mcp_client_oauth.discover(MCP_URL)
    assert found["fallback"] is True
    assert found["authorization_endpoint"] == "https://mcp.example/authorize"
    assert found["token_endpoint"] == "https://mcp.example/token"
    assert found["registration_endpoint"] == "https://mcp.example/register"


@pytest.mark.asyncio
async def test_discover_never_binds_tokens_to_a_foreign_resource(
    fake: FakeDCRServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fake._handle  # noqa: SLF001

    def hostile(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={
                "resource": "https://bank.example/api", "authorization_servers": [ISSUER]})
        return original(request)

    monkeypatch.setattr(fake, "_handle", hostile)
    found = await mcp_client_oauth.discover(MCP_URL)
    assert found["resource"] == MCP_URL


@pytest.mark.asyncio
async def test_discover_refuses_a_plain_http_authorization_endpoint(
    fake: FakeDCRServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fake._handle  # noqa: SLF001

    def downgraded(request: httpx.Request) -> httpx.Response:
        resp = original(request)
        if request.url.path == "/.well-known/oauth-authorization-server":
            meta = resp.json()
            meta["authorization_endpoint"] = "http://auth.example/authorize"
            return httpx.Response(200, json=meta)
        return resp

    monkeypatch.setattr(fake, "_handle", downgraded)
    with pytest.raises(mcp_client_oauth.MCPOAuthError, match="not an https URL"):
        await mcp_client_oauth.discover(MCP_URL)


# ── sign-in + use ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sign_in_registers_once_and_tools_work(fake: FakeDCRServer) -> None:
    record = await _save_oauth_server()
    assert mcp_client_oauth.public_status(record)["connected"] is False

    url, pending = await mcp_client_oauth.begin_sign_in(
        record, "https://deck.example/fd/mcp/oauth/callback", "st-1")
    assert len(fake.registrations) == 1
    reg = fake.registrations[0]
    assert reg["redirect_uris"] == ["https://deck.example/fd/mcp/oauth/callback"]
    assert reg["grant_types"] == ["authorization_code", "refresh_token"]
    assert reg["token_endpoint_auth_method"] == "none"

    code, q = fake.approve(url)
    assert url.startswith(f"{ISSUER}/authorize?")
    assert q["client_id"] == "dyn-1"
    assert q["code_challenge_method"] == "S256"
    assert q["state"] == "st-1"
    assert q["resource"] == MCP_URL
    assert q["scope"] == "read"

    await mcp_client_oauth.complete_sign_in(pending, code, connected_by="pilot@example.com")
    status = mcp_client_oauth.public_status(record)
    assert status["connected"] is True
    assert status["connected_by"] == "pilot@example.com"
    assert status["issuer"] == ISSUER

    grants = mcp_client_oauth._grants_path()  # noqa: SLF001
    assert stat.S_IMODE(os.stat(grants).st_mode) == 0o600
    assert "at-" not in json.dumps(mcp_storage.list_servers_public())  # tokens never in the record

    manager = mcp_manager.get_manager()
    tools = await manager.list_tools("remote")
    assert [t["name"] for t in tools] == ["search"]
    result = await manager.call_tool("remote", "search", {"q": "x"})
    assert result["content"][0]["text"] == "found it"
    assert fake.mcp_auth_headers[-1].startswith("Bearer at-")

    # A second sign-in reuses the registration.
    await mcp_client_oauth.begin_sign_in(record, "https://deck.example/fd/mcp/oauth/callback", "st-2")
    assert len(fake.registrations) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_pre_registered_client_skips_registration(fake: FakeDCRServer) -> None:
    record = await _save_oauth_server(client_id="manual-id")
    url, _ = await mcp_client_oauth.begin_sign_in(record, "https://deck.example/cb", "st")
    assert fake.registrations == []
    assert fake.approve(url)[1]["client_id"] == "manual-id"


@pytest.mark.asyncio
async def test_expiring_token_is_refreshed_once_for_concurrent_callers(fake: FakeDCRServer) -> None:
    record = await _save_oauth_server()
    await _sign_in(fake, record)
    grants = json.loads(mcp_client_oauth._grants_path().read_text())  # noqa: SLF001
    first_refresh = grants["remote"]["refresh_token"]
    grants["remote"]["expires_at"] = 1.0  # long expired
    mcp_client_oauth._write_all(grants)  # noqa: SLF001

    tokens = await asyncio.gather(*(mcp_client_oauth.access_token(record) for _ in range(5)))
    refreshes = [r for r in fake.token_requests if r["grant_type"] == "refresh_token"]
    assert len(refreshes) == 1
    assert refreshes[0]["refresh_token"] == first_refresh
    assert refreshes[0]["resource"] == MCP_URL
    assert len(set(tokens)) == 1
    stored = json.loads(mcp_client_oauth._grants_path().read_text())["remote"]  # noqa: SLF001
    assert stored["refresh_token"] != first_refresh  # rotated one kept


@pytest.mark.asyncio
async def test_401_refreshes_and_retries(fake: FakeDCRServer) -> None:
    record = await _save_oauth_server()
    await _sign_in(fake, record)
    manager = mcp_manager.get_manager()
    await manager.list_tools("remote")
    fake.valid_access.clear()  # the server revoked the access token early

    result = await manager.call_tool("remote", "search", {})
    assert result["content"][0]["text"] == "found it"
    assert [r["grant_type"] for r in fake.token_requests].count("refresh_token") == 1
    await manager.close()


@pytest.mark.asyncio
async def test_dead_refresh_token_asks_for_a_new_sign_in(fake: FakeDCRServer) -> None:
    record = await _save_oauth_server()
    await _sign_in(fake, record)
    fake.valid_access.clear()
    fake.valid_refresh.clear()
    manager = mcp_manager.get_manager()
    with pytest.raises(mcp_manager.MCPServerError, match="sign in"):
        await manager.list_tools("remote")
    status = mcp_client_oauth.public_status(record)
    assert status["connected"] is False
    assert status["registered"] is True  # the next sign-in won't re-register
    await manager.close()


@pytest.mark.asyncio
async def test_not_signed_in_probe_reports_sign_in_readiness(fake: FakeDCRServer) -> None:
    result = await mcp_manager.get_manager().probe_record(
        {"name": "remote", "url": MCP_URL, "auth": "oauth"})
    assert result["ok"] is True
    assert result["needs_sign_in"] is True
    assert result["registration_supported"] is True
    assert result["issuer"] == ISSUER


@pytest.mark.asyncio
async def test_plain_server_401_hints_at_oauth(fake: FakeDCRServer) -> None:
    await mcp_storage.upsert_server({"name": "plain", "url": MCP_URL})
    result = await mcp_manager.get_manager().test_server("plain")
    assert result["ok"] is False
    assert "OAuth sign-in" in result["error"]


# ── routes: the popup flow ──────────────────────────────────────────


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(mcp_routes.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    return TestClient(app)


def test_routes_popup_flow_end_to_end(fake: FakeDCRServer) -> None:
    client = _app()
    resp = client.post("/fd/mcp/servers", json={"name": "remote", "url": MCP_URL, "auth": "oauth"})
    assert resp.status_code == 200
    server = client.get("/fd/mcp/servers").json()["servers"][0]
    assert server["auth"] == "oauth"
    assert server["oauth"]["connected"] is False

    ticket = client.post("/fd/mcp/oauth/connect-ticket", json={"server": "remote"}).json()["ticket"]
    resp = client.get(f"/fd/mcp/oauth/login?ticket={ticket}", follow_redirects=False)
    assert resp.status_code == 302
    code, q = fake.approve(resp.headers["location"])
    assert q["redirect_uri"] == "http://testserver/fd/mcp/oauth/callback"

    resp = client.get(f"/fd/mcp/oauth/callback?code={code}&state={q['state']}")
    assert resp.status_code == 200
    assert "Connected" in resp.text
    server = client.get("/fd/mcp/servers").json()["servers"][0]
    assert server["oauth"]["connected"] is True
    assert server["oauth"]["connected_by"] == USER["email"]
    assert mcp_client_oauth.signer(mcp_storage.get_server("remote")) == (USER["id"], USER["email"])

    resp = client.post("/fd/mcp/servers/remote/test")
    assert resp.json()["ok"] is True and resp.json()["tools_count"] == 1

    assert client.post("/fd/mcp/oauth/disconnect", json={"server": "remote"}).json() == {"ok": True}
    assert fake.revoked  # refresh token revoked at the server
    assert client.get("/fd/mcp/servers").json()["servers"][0]["oauth"]["connected"] is False


def test_routes_refuse_a_ticket_or_state_from_another_browser(fake: FakeDCRServer) -> None:
    client = _app()
    client.post("/fd/mcp/servers", json={"name": "remote", "url": MCP_URL, "auth": "oauth"})

    ticket = client.post("/fd/mcp/oauth/connect-ticket", json={"server": "remote"}).json()["ticket"]
    other = _app()  # no ticket cookie
    resp = other.get(f"/fd/mcp/oauth/login?ticket={ticket}", follow_redirects=False)
    assert resp.status_code == 401

    ticket = client.post("/fd/mcp/oauth/connect-ticket", json={"server": "remote"}).json()["ticket"]
    resp = client.get(f"/fd/mcp/oauth/login?ticket={ticket}", follow_redirects=False)
    code, q = fake.approve(resp.headers["location"])
    resp = other.get(f"/fd/mcp/oauth/callback?code={code}&state={q['state']}")
    assert resp.status_code == 400
    assert mcp_client_oauth.public_status(mcp_storage.get_server("remote"))["connected"] is False


def test_routes_connect_ticket_needs_an_oauth_server(fake: FakeDCRServer) -> None:
    client = _app()
    client.post("/fd/mcp/servers", json={"name": "plain", "url": MCP_URL})
    assert client.post("/fd/mcp/oauth/connect-ticket", json={"server": "plain"}).status_code == 400
    assert client.post("/fd/mcp/oauth/connect-ticket", json={"server": "nope"}).status_code == 404


@pytest.mark.asyncio
async def test_changing_the_url_drops_the_sign_in(fake: FakeDCRServer) -> None:
    record = await _save_oauth_server()
    await _sign_in(fake, record)
    client = _app()
    client.post("/fd/mcp/servers", json={"name": "remote", "url": MCP_URL, "auth": "oauth", "scope": "x"})
    assert mcp_client_oauth.public_status(mcp_storage.get_server("remote"))["connected"] is True
    client.post("/fd/mcp/servers", json={"name": "remote", "url": "https://mcp.example/v2", "auth": "oauth"})
    assert mcp_client_oauth._load_all() == {}  # noqa: SLF001
