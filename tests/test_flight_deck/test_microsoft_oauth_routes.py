"""Microsoft OAuth on Flight Deck (``/fd/microsoft/*``) — the Google twin's
guarantees, through the real routes:

* tokens are per FD user; an agent gets only its OWNER's token, resolved from
  the deck's own records (unknown / missing identity and browsers → 403);
* the sign-in is bound to the browser that started it (connect ticket +
  state cookie), uses the account picker and asks for offline_access;
* rotated refresh tokens are stored; a new client / tenant / scope set
  disconnects everyone; the tenant is validated;
* ``microsoft_oauth:*`` user settings are server-owned (/fd/settings can't
  read or write them); an auth-disabled deck serves nothing.

Microsoft is never contacted: exchange / userinfo / refresh are mocked.
"""

import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.microsoft_oauth_routes as mr
from captain_claw.flight_deck import agent_secret, auth
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.microsoft_oauth import DEFAULT_SCOPES, MicrosoftOAuthTokens

ALICE = "user-alice"  # admin
BOB = "user-bob"


def _tokens(access, refresh=None, *, expires_in=3600):
    return MicrosoftOAuthTokens(
        access_token=access, refresh_token=refresh or f"{access}-refresh",
        expires_at=time.time() + expires_in, scope="openid Mail.ReadWrite User.Read",
    )


@pytest.fixture()
async def db(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    for var in ("FD_LOCKDOWN", "FD_PUBLIC_URL", "FD_AGENT_SHARED_SECRET", "FD_COOKIE_SECURE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    agent_secret.reset_cache_for_tests()
    d = FlightDeckDB(str(tmp_path / "fd.db"))
    await d.init()
    prev_db = auth._db
    auth.set_auth_db(d)
    now = "2026-01-01T00:00:00Z"
    for uid, email, role in [(ALICE, "alice@x.co", "admin"), (BOB, "bob@x.co", "user")]:
        await d._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, email, "h", uid.title(), role, now, now),
        )
    await d._db.commit()
    await d.set_system_setting(mr._K_CLIENT_ID, "cid")
    await d.set_system_setting(mr._K_CLIENT_SECRET, "csecret")
    mr._pending_oauth.clear()
    mr._connect_tickets.clear()
    mr._refresh_locks.clear()
    try:
        yield d
    finally:
        mr._pending_oauth.clear()
        mr._connect_tickets.clear()
        agent_secret.reset_cache_for_tests()
        auth.set_auth_db(prev_db)
        await d.close()


@pytest.fixture()
def agents(monkeypatch):
    import captain_claw.flight_deck.server as srv
    registry = {"tok-alice": ALICE, "tok-bob": BOB}
    monkeypatch.setattr(
        srv, "_resolve_agent_identity_by_auth",
        lambda t: (t in registry, registry.get(t, "")) if t else (False, ""),
    )
    return registry


@pytest.fixture()
def microsoft(monkeypatch):
    calls = {"exchange": [], "refresh": []}

    async def exchange(**kw):
        calls["exchange"].append(kw)
        return _tokens("fresh-access", "fresh-refresh")

    async def userinfo(access_token):
        return {"sub": "ms-bob", "email": "bob@outlook.com", "name": "Bob", "upn": "bob@outlook.com"}

    async def refresh(**kw):
        calls["refresh"].append(kw)
        return _tokens("rotated-access", "rotated-refresh")

    monkeypatch.setattr(mr, "exchange_code_for_tokens", exchange)
    monkeypatch.setattr(mr, "fetch_user_info", userinfo)
    monkeypatch.setattr(mr, "refresh_access_token", refresh)
    return calls


def _client(host="127.0.0.1"):
    app = FastAPI()
    app.include_router(mr.router)
    return TestClient(app, client=(host, 50123), follow_redirects=False)


def _bearer(uid, role="user"):
    return {"Authorization": f"Bearer {auth.create_access_token(uid, role)}"}


def _ticket(client, uid):
    r = client.post("/fd/microsoft/connect-ticket", headers=_bearer(uid))
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


class TestAgentToken:
    async def test_each_agent_gets_only_its_owners_token(self, db, agents):
        await mr._store_tokens(db, ALICE, _tokens("ALICE-ACCESS"))
        await mr._store_tokens(db, BOB, _tokens("BOB-ACCESS"))
        c = _client()
        r = c.get("/fd/microsoft/access_token", headers={"X-Agent-Auth": "tok-bob"})
        assert r.status_code == 200 and r.json()["access_token"] == "BOB-ACCESS"
        assert "refresh" not in r.text  # the refresh token never leaves the deck

    async def test_unconnected_owner_gets_404_not_someone_else(self, db, agents):
        await mr._store_tokens(db, ALICE, _tokens("ALICE-ACCESS"))
        r = _client().get("/fd/microsoft/access_token", headers={"X-Agent-Auth": "tok-bob"})
        assert r.status_code == 404 and "ALICE" not in r.text

    async def test_unknown_or_missing_identity_is_refused(self, db, agents):
        await mr._store_tokens(db, ALICE, _tokens("ALICE-ACCESS"))
        c = _client()
        for headers in ({}, {"X-Agent-Auth": "other-deck"}):
            r = c.get("/fd/microsoft/access_token", headers=headers)
            assert r.status_code == 403 and "ALICE" not in r.text
        assert "Microsoft connections" in c.get("/fd/microsoft/access_token").json()["detail"]

    async def test_browser_request_is_refused(self, db, agents):
        await mr._store_tokens(db, BOB, _tokens("BOB-ACCESS"))
        r = _client().get("/fd/microsoft/access_token",
                          headers={"X-Agent-Auth": "tok-bob", "Sec-Fetch-Mode": "cors"})
        assert r.status_code == 403 and "BOB" not in r.text

    async def test_expired_token_is_refreshed_and_the_rotated_one_stored(self, db, agents, microsoft):
        await mr._store_tokens(db, BOB, _tokens("old", "old-refresh", expires_in=-10))
        r = _client().get("/fd/microsoft/access_token", headers={"X-Agent-Auth": "tok-bob"})
        assert r.json()["access_token"] == "rotated-access"
        assert microsoft["refresh"][0]["refresh_token"] == "old-refresh"
        stored = await mr._load_tokens(db, BOB)
        assert stored.refresh_token == "rotated-refresh"

    async def test_failed_refresh_is_401_with_a_reconnect_hint(self, db, agents, monkeypatch):
        async def boom(**kw):
            raise RuntimeError("AADSTS70008: expired")
        monkeypatch.setattr(mr, "refresh_access_token", boom)
        await mr._store_tokens(db, BOB, _tokens("old", expires_in=-10))
        r = _client().get("/fd/microsoft/access_token", headers={"X-Agent-Auth": "tok-bob"})
        assert r.status_code == 401 and "Connections → Microsoft" in r.json()["detail"]

    async def test_agent_status(self, db, agents):
        await mr._store_tokens(db, BOB, _tokens("BOB"))
        c = _client()
        assert c.get("/fd/microsoft/agent_status", headers={"X-Agent-Auth": "tok-bob"}).json() == \
            {"connected": True, "enabled": True}
        assert c.get("/fd/microsoft/agent_status", headers={"X-Agent-Auth": "tok-alice"}).json()["connected"] is False


class TestSignIn:
    async def test_login_redirects_to_microsoft_with_picker_pkce_and_offline(self, db):
        c = _client()
        r = c.get(f"/fd/microsoft/login?ticket={_ticket(c, BOB)}")
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
        q = parse_qs(urlsplit(loc).query)
        assert q["prompt"] == ["select_account"]
        assert q["code_challenge_method"] == ["S256"]
        assert "offline_access" in q["scope"][0].split()
        assert q["redirect_uri"] == ["http://testserver/fd/microsoft/callback"]
        assert mr._pending_oauth[q["state"][0]]["owner"] == BOB
        cookie = next(v.lower() for v in r.headers.get_list("set-cookie")
                      if v.startswith(f"{mr._STATE_COOKIE}="))
        assert "httponly" in cookie and "path=/fd/microsoft" in cookie

    async def test_tenant_goes_into_the_authority(self, db):
        await db.set_system_setting(mr._K_TENANT, "contoso.onmicrosoft.com")
        c = _client()
        loc = c.get(f"/fd/microsoft/login?ticket={_ticket(c, BOB)}").headers["location"]
        assert loc.startswith("https://login.microsoftonline.com/contoso.onmicrosoft.com/")

    async def test_full_flow_stores_tokens_for_the_tickets_user(self, db, microsoft):
        c = _client()
        loc = c.get(f"/fd/microsoft/login?ticket={_ticket(c, BOB)}").headers["location"]
        state = parse_qs(urlsplit(loc).query)["state"][0]
        r = c.get(f"/fd/microsoft/callback?code=abc&state={state}")
        assert r.status_code == 200
        assert '"captain-claw-microsoft-oauth"' in r.text and "bob@outlook.com" in r.text
        assert microsoft["exchange"][0]["code_verifier"]
        assert (await mr._load_tokens(db, BOB)).refresh_token == "fresh-refresh"
        assert await mr._load_tokens(db, ALICE) is None
        status = c.get("/fd/microsoft/status", headers=_bearer(BOB)).json()
        assert status["connected"] is True and status["user"]["email"] == "bob@outlook.com"
        assert {"scope": "Mail.ReadWrite", "label": "Mail (read + drafts)"} in status["granted_scopes"]

    async def test_callback_in_another_browser_saves_nothing(self, db, microsoft):
        bob, other = _client(), _client()
        loc = bob.get(f"/fd/microsoft/login?ticket={_ticket(bob, BOB)}").headers["location"]
        state = parse_qs(urlsplit(loc).query)["state"][0]
        r = other.get(f"/fd/microsoft/callback?code=abc&state={state}")
        assert r.status_code == 400 and "different browser" in r.text
        assert microsoft["exchange"] == [] and await mr._load_tokens(db, BOB) is None

    async def test_forwarded_login_link_is_refused(self, db):
        link = f"/fd/microsoft/login?ticket={_ticket(_client(), BOB)}"
        r = _client().get(link)
        assert r.status_code == 401 and mr._pending_oauth == {}

    async def test_error_description_is_escaped(self, db):
        r = _client().get("/fd/microsoft/callback?error=x&error_description=%3Cscript%3Ealert(1)%3C/script%3E")
        assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text

    async def test_logout_clears_only_this_user_and_says_where_to_revoke(self, db):
        await mr._store_tokens(db, ALICE, _tokens("A"))
        await mr._store_tokens(db, BOB, _tokens("B"))
        r = _client().post("/fd/microsoft/logout", headers=_bearer(BOB))
        assert r.json()["disconnected"] is True and "account.live.com" in r.json()["note"]
        assert await mr._load_tokens(db, BOB) is None
        assert await mr._load_tokens(db, ALICE) is not None


class TestConfig:
    async def test_admin_only(self, db):
        r = _client().post("/fd/microsoft/config", json={"tenant": "organizations"}, headers=_bearer(BOB))
        assert r.status_code == 403

    async def test_get_never_returns_the_secret(self, db):
        r = _client().get("/fd/microsoft/config", headers=_bearer(BOB)).json()
        assert r["client_secret_set"] is True and "csecret" not in json.dumps(r)
        assert r["tenant"] == "common" and r["scopes"] == DEFAULT_SCOPES

    async def test_bad_tenant_is_refused(self, db):
        r = _client().post("/fd/microsoft/config", json={"tenant": "evil/../x"},
                           headers=_bearer(ALICE, "admin"))
        assert r.status_code == 400

    @pytest.mark.parametrize("change", [{"client_id": "other"}, {"tenant": "consumers"},
                                        {"scopes": ["Mail.Read"]}])
    async def test_new_client_tenant_or_scopes_disconnect_everyone(self, db, change):
        await mr._store_tokens(db, ALICE, _tokens("A"))
        await mr._store_tokens(db, BOB, _tokens("B"))
        r = _client().post("/fd/microsoft/config", json=change, headers=_bearer(ALICE, "admin"))
        assert r.json()["disconnected_all"] is True
        assert await mr._load_tokens(db, ALICE) is None and await mr._load_tokens(db, BOB) is None

    async def test_same_values_keep_connections(self, db):
        await mr._store_tokens(db, BOB, _tokens("B"))
        r = _client().post("/fd/microsoft/config", json={"client_id": "cid", "client_secret": "new"},
                           headers=_bearer(ALICE, "admin"))
        assert r.json()["disconnected_all"] is False
        assert await mr._load_tokens(db, BOB) is not None

    async def test_required_scopes_are_always_kept(self, db):
        _client().post("/fd/microsoft/config", json={"scopes": ["Calendars.Read"]},
                       headers=_bearer(ALICE, "admin"))
        scopes = (await mr._load_client(db))["scopes"]
        assert {"offline_access", "User.Read", "openid", "Calendars.Read"} <= set(scopes)


class TestServerOwnedSettings:
    def test_settings_route_treats_microsoft_tokens_as_server_owned(self):
        from captain_claw.flight_deck import settings_routes

        assert settings_routes._server_owned("microsoft_oauth:tokens")
        assert settings_routes._server_owned("Microsoft_OAuth:user")


class TestAuthDisabledDeck:
    async def test_every_route_is_refused(self, db, agents, monkeypatch):
        monkeypatch.setenv("FD_AUTH_ENABLED", "false")
        c = _client()
        r = c.get("/fd/microsoft/access_token", headers={"X-Agent-Auth": "tok-bob"})
        assert r.status_code == 403 and r.headers["X-FD-Microsoft-Unavailable"] == "auth-disabled"
        assert c.get("/fd/microsoft/status").status_code == 503


class TestMailSignature:
    async def test_per_user_and_server_owned(self, db):
        from captain_claw import mail_signature
        from captain_claw.flight_deck.settings_routes import _server_owned

        c = _client()
        assert c.get("/fd/microsoft/mail-signature").status_code == 401
        assert c.get("/fd/microsoft/mail-signature", headers=_bearer(BOB)).json() == {"signature": ""}
        r = c.put("/fd/microsoft/mail-signature", headers=_bearer(BOB),
                  json={"signature": " Bob's agent \r\n"})
        assert r.status_code == 200 and r.json() == {"signature": "Bob's agent"}
        assert c.get("/fd/microsoft/mail-signature", headers=_bearer(BOB)).json() == \
            {"signature": "Bob's agent"}
        assert c.get("/fd/microsoft/mail-signature", headers=_bearer(ALICE)).json() == {"signature": ""}
        assert c.put("/fd/microsoft/mail-signature", headers=_bearer(BOB),
                     json={"signature": ""}).json() == {"signature": ""}
        assert _server_owned(mail_signature.MICROSOFT_KEY)

    @pytest.mark.parametrize("value", [7, {"a": 1}, "x" * 1001])
    async def test_bad_values_are_a_400(self, db, value):
        r = _client().put("/fd/microsoft/mail-signature", headers=_bearer(BOB), json={"signature": value})
        assert r.status_code == 400 and "signature" in r.json()["detail"]

    async def test_agent_status_carries_the_owners_only_when_set(self, db, agents):
        c = _client()
        c.put("/fd/microsoft/mail-signature", headers=_bearer(BOB), json={"signature": "Bob's agent"})
        assert c.get("/fd/microsoft/agent_status", headers={"X-Agent-Auth": "tok-bob"}).json() == \
            {"connected": False, "enabled": True, "mail_signature": "Bob's agent"}
        assert "mail_signature" not in c.get(
            "/fd/microsoft/agent_status", headers={"X-Agent-Auth": "tok-alice"}).json()
