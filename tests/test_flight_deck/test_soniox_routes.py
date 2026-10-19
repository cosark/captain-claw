"""Soniox on Flight Deck (``/fd/soniox/*``), through the real routes:

* the key is per FD user, verified before it is saved, and never returned —
  only a ``····ab12`` hint; ``soniox:*`` settings are server-owned;
* whose key a use takes: own → the admin's team key → ``SONIOX_API_KEY``;
  a surface with no person behind it takes the team key, then the env;
* an admin moves the env key into Flight Deck with one call;
* an agent never receives a stored key: it transcribes through Flight Deck
  with its OWNER's key, and a realtime session gets a short-lived key;
* agents no longer inherit FD's ``SONIOX_API_KEY``;
* the agent-side client goes through Flight Deck, or uses a key the agent was
  given on purpose.

Soniox is never contacted: ``httpx.AsyncClient`` gets a mock transport.
"""

from __future__ import annotations

import json
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.soniox_routes as sr
from captain_claw import soniox_api, soniox_client
from captain_claw.flight_deck import agent_secret, auth, settings_routes
from captain_claw.flight_deck.db import FlightDeckDB

ALICE = "user-alice"
BOB = "user-bob"
KEY_ALICE = "snx_alice_0000aaaa"
KEY_BOB = "snx_bob_0000bbbb"
KEY_TEAM = "snx_team_0000tttt"
KEY_ENV = "snx_env_0000eeee"
GOOD_KEYS = {KEY_ALICE, KEY_BOB, KEY_TEAM, KEY_ENV}
MODELS = [{"id": "stt-async-v4", "name": "Async v4"}, {"id": "stt-rt-v4", "name": "Realtime v4"}]
TOKENS = [{"text": "Hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9},
          {"text": " world", "start_ms": 400, "end_ms": 900, "confidence": 0.9}]


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
async def db(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    for var in ("FD_LOCKDOWN", "FD_AGENT_SHARED_SECRET", "SONIOX_API_KEY", "SONIOX_API_BASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    sr._ENV_META.clear()
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
    try:
        yield d
    finally:
        agent_secret.reset_cache_for_tests()
        auth.set_auth_db(prev_db)
        await d.close()


@pytest.fixture()
def soniox(monkeypatch):
    """A fake Soniox behind every ``httpx.AsyncClient``: records each request;
    ``state.fail`` forces an outcome ("network", or an HTTP status)."""
    state = types.SimpleNamespace(requests=[], fail=None, job_status="completed")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        state.requests.append({
            "method": request.method, "path": path,
            "auth": request.headers.get("authorization", ""),
            "body": request.content if path.startswith("/v1/auth") or path == "/v1/transcriptions" else b"",
        })
        if state.fail == "network":
            raise httpx.ConnectError("boom", request=request)
        if isinstance(state.fail, int):
            return httpx.Response(state.fail, json={"message": "nope"})
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in GOOD_KEYS:
            return httpx.Response(401, json={"message": "Incorrect API key provided."})
        if request.method == "DELETE":
            return httpx.Response(204)
        if path == "/v1/models":
            return httpx.Response(200, json={"models": MODELS})
        if path == "/v1/files":
            return httpx.Response(201, json={"id": "file-1"})
        if path == "/v1/transcriptions":
            return httpx.Response(201, json={"id": "tr-1"})
        if path == "/v1/transcriptions/tr-1":
            return httpx.Response(200, json={"status": state.job_status, "error_message": "bad audio"})
        if path == "/v1/transcriptions/tr-1/transcript":
            return httpx.Response(200, json={"text": "Hello world", "tokens": TOKENS})
        if path == "/tts":
            return httpx.Response(200, content=b"ID3-speech:" + request.content[:40])
        if path == "/v1/auth/temporary-api-key":
            return httpx.Response(201, json={"api_key": "temp:abc123", "expires_at": "2026-10-19T12:10:00Z"})
        return httpx.Response(404, json={"message": "no route"})

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        kw.setdefault("transport", httpx.MockTransport(handler))
        return real_client(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


@pytest.fixture()
def agents(monkeypatch):
    """This deck's agents: web_auth token → (owner, slug)."""
    import captain_claw.flight_deck.server as srv

    registry = {"tok-alice": (ALICE, "alice-bot"), "tok-bob": (BOB, "bob-bot"),
                "tok-local": ("", "desk-bot")}

    def find(token):
        if token in registry:
            owner, slug = registry[token]
            return True, owner, slug
        return False, "", ""

    monkeypatch.setattr(srv, "_find_agent_by_auth", find)
    monkeypatch.setattr(srv, "_resolve_agent_identity_by_auth", lambda t: find(t)[:2])
    return registry


def _client():
    app = FastAPI()
    app.include_router(sr.router)
    app.include_router(settings_routes.router)
    return TestClient(app, client=("127.0.0.1", 50123))


def _bearer(uid, role="user"):
    return {"Authorization": f"Bearer {auth.create_access_token(uid, role)}"}


async def _save(owner, key, **meta):
    await sr._write(owner, {sr._K_KEY: key, sr._K_META: json.dumps(
        {"verified": True, "verified_at": "2026-10-01T00:00:00+00:00",
         "updated_at": "2026-10-01T00:00:00+00:00", "models": ["stt-async-v4"], **meta})})


async def _share(key=KEY_TEAM):
    db = auth.get_db()
    await db.set_system_setting(sr._K_SHARED_KEY, key)
    await db.set_system_setting(sr._K_SHARED_META, json.dumps({"verified": True}))


def _soniox_calls(state, path):
    return [r for r in state.requests if r["path"] == path]


async def _usage(db, uid):
    return [json.loads(r["detail"]) for r in await db.get_usage_logs(user_id=uid, event_type="soniox_call")]


# ── the key ─────────────────────────────────────────────────────────


class TestKey:
    async def test_status_without_any_key(self, db):
        r = _client().get("/fd/soniox/status", headers=_bearer(BOB))
        assert r.status_code == 200
        assert r.json() == {
            "key_set": False, "key_hint": "", "verified": None, "verify_error": None,
            "verified_at": None, "updated_at": None, "models": [], "source": None,
            "shared_available": False, "auth_enabled": True, "scope": "user",
        }

    async def test_save_verifies_and_never_returns_the_key(self, db, soniox):
        c = _client()
        r = c.put("/fd/soniox/key", headers=_bearer(BOB), json={"api_key": f"  {KEY_BOB}  "})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["key_set"] is True and body["verified"] is True and body["source"] == "own"
        assert body["key_hint"] == "····bbbb" and body["models"] == ["stt-async-v4", "stt-rt-v4"]
        assert soniox.requests[-1]["path"] == "/v1/models"
        assert soniox.requests[-1]["auth"] == f"Bearer {KEY_BOB}"
        assert KEY_BOB not in r.text
        assert await sr.key_for(BOB) == KEY_BOB
        assert await sr.key_for(ALICE) == ""

    async def test_a_key_soniox_refuses_is_a_400_and_not_saved(self, db, soniox):
        r = _client().put("/fd/soniox/key", headers=_bearer(BOB), json={"api_key": "snx_wrong_key_123"})
        assert r.status_code == 400 and r.json()["detail"].startswith("Soniox refused this key")
        assert "snx_wrong_key_123" not in r.text
        assert await sr.key_for(BOB) == ""

    @pytest.mark.parametrize("fail", ["network", 503])
    async def test_unverifiable_key_is_saved_with_a_warning(self, db, soniox, fail):
        soniox.fail = fail
        r = _client().put("/fd/soniox/key", headers=_bearer(BOB), json={"api_key": KEY_BOB})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["verified"] is False and body["verify_error"] == "unreachable"
        assert body["warning"].startswith("Saved, but couldn't verify it with Soniox")
        assert await sr.key_for(BOB) == KEY_BOB

    @pytest.mark.parametrize("payload", [{"api_key": "   "}, {"api_key": ["x"]}, {"key": KEY_BOB}])
    async def test_blank_or_malformed_is_a_422(self, db, soniox, payload):
        r = _client().put("/fd/soniox/key", headers=_bearer(BOB), json=payload)
        assert r.status_code == 422 and r.json()["detail"] == "Paste a Soniox API key"
        assert soniox.requests == []

    async def test_impossible_characters_are_refused_before_soniox(self, db, soniox):
        r = _client().put("/fd/soniox/key", headers=_bearer(BOB), json={"api_key": KEY_BOB + "​"})
        assert r.status_code == 422 and "Copy it again" in r.json()["detail"]
        assert soniox.requests == []

    async def test_delete_forgets_only_this_users_key(self, db, soniox):
        await _save(BOB, KEY_BOB)
        await _save(ALICE, KEY_ALICE)
        r = _client().delete("/fd/soniox/key", headers=_bearer(BOB))
        assert r.status_code == 200 and r.json()["key_set"] is False
        assert await db.get_setting(BOB, sr._K_KEY) is None
        assert await sr.key_for(ALICE) == KEY_ALICE

    async def test_test_route(self, db, soniox):
        c = _client()
        assert c.post("/fd/soniox/test", headers=_bearer(BOB)).status_code == 404
        await _save(BOB, KEY_BOB, verified=False, models=[])
        r = c.post("/fd/soniox/test", headers=_bearer(BOB))
        assert r.status_code == 200 and r.json()["models"] == ["stt-async-v4", "stt-rt-v4"]
        meta = json.loads(await db.get_setting(BOB, sr._K_META))
        assert meta["verified"] is True
        await _save(BOB, "snx_revoked_key_99")
        r = c.post("/fd/soniox/test", headers=_bearer(BOB))
        assert r.status_code == 400  # never 401: that reads as an expired FD session
        assert c.get("/fd/soniox/status", headers=_bearer(BOB)).json()["verify_error"] == "refused"

    async def test_soniox_settings_are_server_owned(self, db):
        await _save(BOB, KEY_BOB)
        await db.set_settings(BOB, {"theme": "dark"})
        c = _client()
        got = c.get("/fd/settings", headers=_bearer(BOB))
        assert got.json() == {"theme": "dark"} and KEY_BOB not in got.text
        r = c.put("/fd/settings", headers=_bearer(BOB), json={"settings": {"soniox:api_key": "forged"}})
        assert r.status_code == 400 and "soniox:api_key" in r.json()["detail"]


# ── whose key ───────────────────────────────────────────────────────


class TestResolution:
    async def test_own_then_team_then_env(self, db, monkeypatch):
        monkeypatch.setenv("SONIOX_API_KEY", KEY_ENV)
        assert await sr._resolve(BOB) == (KEY_ENV, "env")
        await _share()
        assert await sr._resolve(BOB) == (KEY_TEAM, "shared")
        await _save(BOB, KEY_BOB)
        assert await sr._resolve(BOB) == (KEY_BOB, "own")
        status = _client().get("/fd/soniox/status", headers=_bearer(BOB)).json()
        assert status["source"] == "own" and status["shared_available"] is True

    async def test_a_surface_with_no_person_takes_the_team_key_then_env(self, db, monkeypatch):
        await _save(BOB, KEY_BOB)
        assert await sr.key_for(None) == ""  # never someone's own key
        monkeypatch.setenv("SONIOX_API_KEY", KEY_ENV)
        assert await sr.key_for(None) == KEY_ENV
        await _share()
        assert await sr.key_for(None) == KEY_TEAM

    async def test_env_status_is_reported_as_env(self, db, monkeypatch):
        monkeypatch.setenv("SONIOX_API_KEY", KEY_ENV)
        r = _client().get("/fd/soniox/status", headers=_bearer(BOB))
        assert r.json()["source"] == "env" and r.json()["key_hint"] == "····eeee"
        assert KEY_ENV not in r.text

    async def test_auth_off_deck_has_one_key_and_no_team_key(self, db, soniox, monkeypatch):
        monkeypatch.setenv("FD_AUTH_ENABLED", "false")
        c = _client()
        r = c.put("/fd/soniox/key", json={"api_key": KEY_ALICE})
        assert r.status_code == 200 and r.json()["scope"] == "deck"
        assert await db.get_system_setting(sr._K_KEY) == KEY_ALICE
        assert await sr.key_for(None) == KEY_ALICE == await sr.key_for("local")
        assert c.put("/fd/soniox/shared-key", json={"api_key": KEY_TEAM}).status_code == 409


# ── the team key and moving the env key in ──────────────────────────


class TestTeamKey:
    async def test_admin_shares_a_key_users_without_their_own_use(self, db, soniox):
        c = _client()
        r = c.put("/fd/soniox/shared-key", headers=_bearer(ALICE, "admin"), json={"api_key": KEY_TEAM})
        assert r.status_code == 200, r.text
        assert r.json()["key_set"] is True and KEY_TEAM not in r.text
        assert await sr.key_for(BOB) == KEY_TEAM
        assert c.get("/fd/soniox/status", headers=_bearer(BOB)).json()["source"] == "shared"
        assert await db.get_setting(BOB, sr._K_KEY) is None
        r = c.delete("/fd/soniox/shared-key", headers=_bearer(ALICE, "admin"))
        assert r.json()["key_set"] is False and await sr.key_for(BOB) == ""

    async def test_only_admins(self, db, soniox, monkeypatch):
        monkeypatch.setenv("SONIOX_API_KEY", KEY_ENV)
        c = _client()
        assert c.get("/fd/soniox/shared", headers=_bearer(BOB)).status_code == 403
        assert c.put("/fd/soniox/shared-key", headers=_bearer(BOB), json={"api_key": KEY_TEAM}).status_code == 403
        assert c.post("/fd/soniox/import-env", headers=_bearer(BOB)).status_code == 403
        assert await db.get_system_setting(sr._K_SHARED_KEY) is None

    async def test_import_env_moves_the_key_into_flight_deck(self, db, soniox, monkeypatch):
        c = _client()
        admin = _bearer(ALICE, "admin")
        assert c.post("/fd/soniox/import-env", headers=admin).status_code == 404
        monkeypatch.setenv("SONIOX_API_KEY", f" {KEY_ENV} ")
        shared = c.get("/fd/soniox/shared", headers=admin).json()
        assert shared["env_key_set"] is True and shared["env_key_hint"] == "····eeee"
        assert shared["key_set"] is False
        r = c.post("/fd/soniox/import-env", headers=admin)
        assert r.status_code == 200, r.text
        assert r.json()["key_set"] is True and r.json()["verified"] is True and KEY_ENV not in r.text
        assert await db.get_system_setting(sr._K_SHARED_KEY) == KEY_ENV
        # The env can go now: the team key carries on.
        monkeypatch.delenv("SONIOX_API_KEY")
        assert await sr.key_for(BOB) == KEY_ENV
        assert c.get("/fd/soniox/status", headers=_bearer(BOB)).json()["source"] == "shared"

    async def test_import_env_on_an_auth_off_deck_sets_the_deck_key(self, db, soniox, monkeypatch):
        monkeypatch.setenv("FD_AUTH_ENABLED", "false")
        monkeypatch.setenv("SONIOX_API_KEY", KEY_ENV)
        r = _client().post("/fd/soniox/import-env")
        assert r.status_code == 200 and r.json()["source"] == "own" and r.json()["scope"] == "deck"
        assert await db.get_system_setting(sr._K_KEY) == KEY_ENV

    async def test_a_refused_env_key_is_not_imported(self, db, soniox, monkeypatch):
        monkeypatch.setenv("SONIOX_API_KEY", "snx_dead_env_key")
        r = _client().post("/fd/soniox/import-env", headers=_bearer(ALICE, "admin"))
        assert r.status_code == 400 and await db.get_system_setting(sr._K_SHARED_KEY) is None


# ── agents ──────────────────────────────────────────────────────────


class TestAgent:
    async def test_agent_transcribes_with_its_owners_key(self, db, soniox, agents):
        await _save(ALICE, KEY_ALICE)
        await _save(BOB, KEY_BOB)
        r = _client().post(
            "/fd/soniox/agent/transcribe?filename=clip.ogg&language_hints=en,hr",
            headers={"X-Agent-Auth": "tok-bob", "Content-Type": "audio/ogg"}, content=b"OggS-audio",
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["text"] == "Hello world"
        assert body["tokens"][1] == {"text": " world", "start_ms": 400, "end_ms": 900}
        assert KEY_BOB not in r.text
        assert {c["auth"] for c in soniox.requests} == {f"Bearer {KEY_BOB}"}
        (job,) = _soniox_calls(soniox, "/v1/transcriptions")
        assert json.loads(job["body"])["language_hints"] == ["en", "hr"]
        # The file and the job are deleted afterwards.
        assert [c["path"] for c in soniox.requests if c["method"] == "DELETE"] == [
            "/v1/transcriptions/tr-1", "/v1/files/file-1"]
        (logged,) = await _usage(db, BOB)
        assert logged["kind"] == "stt" and logged["agent"] == "bob-bot" and logged["ok"] is True

    async def test_owner_with_no_key_is_404_never_someone_elses(self, db, soniox, agents):
        await _save(ALICE, KEY_ALICE)
        c = _client()
        r = c.post("/fd/soniox/agent/transcribe", headers={"X-Agent-Auth": "tok-bob"}, content=b"x")
        assert r.status_code == 404 and r.json()["detail"] == sr.NOT_CONNECTED
        assert c.get("/fd/soniox/agent/status", headers={"X-Agent-Auth": "tok-bob"}).json() == {"connected": False}
        assert c.get("/fd/soniox/agent/status", headers={"X-Agent-Auth": "tok-alice"}).json() == {"connected": True}
        assert soniox.requests == []

    async def test_agent_uses_the_team_key(self, db, soniox, agents):
        await _share()
        r = _client().post("/fd/soniox/agent/transcribe", headers={"X-Agent-Auth": "tok-bob"}, content=b"x")
        assert r.status_code == 200 and KEY_TEAM not in r.text
        assert soniox.requests[0]["auth"] == f"Bearer {KEY_TEAM}"

    async def test_unknown_identity_and_browsers_are_refused(self, db, soniox, agents):
        await _save(BOB, KEY_BOB)
        c = _client()
        for headers in ({}, {"X-Agent-Auth": "other-deck"},
                        {"X-Agent-Auth": "tok-bob", "Origin": "http://localhost:25080"}):
            assert c.post("/fd/soniox/agent/transcribe", headers=headers, content=b"x").status_code == 403
            assert c.post("/fd/soniox/agent/realtime-key", headers=headers, json={}).status_code == 403
            assert c.get("/fd/soniox/agent/status", headers=headers).status_code == 403
        assert soniox.requests == []

    async def test_realtime_key_is_short_lived_and_never_the_owners(self, db, soniox, agents):
        await _save(BOB, KEY_BOB)
        r = _client().post("/fd/soniox/agent/realtime-key", headers={"X-Agent-Auth": "tok-bob"},
                           json={"usage_type": "transcribe_websocket", "expires_in_seconds": 99999})
        assert r.status_code == 200, r.text
        assert r.json() == {"api_key": "temp:abc123", "expires_at": "2026-10-19T12:10:00Z"}
        (sent,) = _soniox_calls(soniox, "/v1/auth/temporary-api-key")
        assert sent["auth"] == f"Bearer {KEY_BOB}"
        payload = json.loads(sent["body"])
        assert payload["usage_type"] == "transcribe_websocket"
        assert payload["expires_in_seconds"] == 3600  # capped
        assert KEY_BOB not in r.text

    async def test_agent_speaks_with_its_owners_key(self, db, soniox, agents):
        await _save(ALICE, KEY_ALICE)
        await _save(BOB, KEY_BOB)
        r = _client().post("/fd/soniox/agent/speak", headers={"X-Agent-Auth": "tok-bob"},
                           json={"text": "Dobro jutro", "voice": "Emma", "language": "hr"})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "audio/mpeg" and r.content.startswith(b"ID3-speech:")
        (sent,) = [c for c in soniox.requests if c["path"] == "/tts"]
        assert sent["auth"] == f"Bearer {KEY_BOB}"
        (logged,) = await _usage(db, BOB)
        assert logged["kind"] == "tts" and logged["chars"] == len("Dobro jutro") and logged["ok"] is True

    async def test_speak_needs_a_key_text_and_an_agent(self, db, soniox, agents):
        c = _client()
        r = c.post("/fd/soniox/agent/speak", headers={"X-Agent-Auth": "tok-bob"}, json={"text": "hi"})
        assert r.status_code == 404 and r.json()["detail"] == sr.NOT_CONNECTED
        await _save(BOB, KEY_BOB)
        r = c.post("/fd/soniox/agent/speak", headers={"X-Agent-Auth": "tok-bob"}, json={"text": "  "})
        assert r.status_code == 422
        r = c.post("/fd/soniox/agent/speak", headers={"X-Agent-Auth": "tok-bob", "Origin": "http://x"},
                   json={"text": "hi"})
        assert r.status_code == 403
        assert [c for c in soniox.requests if c["path"] == "/tts"] == []

    async def test_bad_usage_type_is_a_422(self, db, soniox, agents):
        await _save(BOB, KEY_BOB)
        r = _client().post("/fd/soniox/agent/realtime-key", headers={"X-Agent-Auth": "tok-bob"},
                           json={"usage_type": "everything"})
        assert r.status_code == 422 and soniox.requests == []

    async def test_a_revoked_key_is_a_400_and_marked_refused(self, db, soniox, agents):
        await _save(BOB, "snx_revoked_key_99")
        r = _client().post("/fd/soniox/agent/transcribe", headers={"X-Agent-Auth": "tok-bob"}, content=b"x")
        assert r.status_code == 400 and "refused the API key" in r.json()["detail"]
        assert json.loads(await db.get_setting(BOB, sr._K_META))["verify_error"] == "refused"

    async def test_a_failed_job_is_a_502_with_sonioxs_reason(self, db, soniox, agents):
        await _save(BOB, KEY_BOB)
        soniox.job_status = "error"
        r = _client().post("/fd/soniox/agent/transcribe", headers={"X-Agent-Auth": "tok-bob"}, content=b"x")
        assert r.status_code == 502 and "bad audio" in r.json()["detail"]
        assert len([c for c in soniox.requests if c["method"] == "DELETE"]) == 2

    async def test_auth_off_agents_use_the_deck_key(self, db, soniox, agents, monkeypatch):
        monkeypatch.setenv("FD_AUTH_ENABLED", "false")
        await _save("local", KEY_ALICE)
        c = _client()
        assert c.get("/fd/soniox/agent/status", headers={"X-Agent-Auth": "tok-local"}).json() == {"connected": True}
        assert c.get("/fd/soniox/agent/status").status_code == 403


# ── Flight Deck's own surfaces ──────────────────────────────────────


class TestBridges:
    async def test_transcribe_text_reasons(self, db, soniox):
        assert await sr.transcribe_text(None, b"x") == ("", sr.NOT_CONNECTED_DECK)
        assert await sr.transcribe_text(BOB, b"x") == ("", sr.NOT_CONNECTED)
        await _save(BOB, KEY_BOB)
        assert await sr.transcribe_text(BOB, b"x") == ("Hello world", "")

    async def test_whatsapp_uses_its_agents_owner(self, db, soniox, monkeypatch):
        import captain_claw.flight_deck.server as srv
        from captain_claw.flight_deck import whatsapp_bridge as wb

        await _save(BOB, KEY_BOB)
        await _share()
        monkeypatch.setattr(srv, "_load_process_registry", lambda: {"bob-bot": {"owner": BOB}})
        monkeypatch.setenv("WHATSAPP_DEFAULT_AGENT_SLUG", "bob-bot")
        assert await wb._transcribe_soniox(b"ogg", "audio/ogg") == ("Hello world", "")
        assert soniox.requests[0]["auth"] == f"Bearer {KEY_BOB}"
        soniox.requests.clear()
        monkeypatch.setenv("WHATSAPP_DEFAULT_AGENT_SLUG", "")
        assert await wb._transcribe_soniox(b"ogg", "audio/ogg") == ("Hello world", "")
        assert soniox.requests[0]["auth"] == f"Bearer {KEY_TEAM}"

    async def test_glasses_tts_without_a_deck_key_is_a_503(self, db, monkeypatch):
        from captain_claw.flight_deck import glasses_bridge as gb

        monkeypatch.delenv("FD_GLASSES_BRIDGE_TOKEN", raising=False)
        app = FastAPI()
        app.include_router(gb.router)
        r = TestClient(app).post("/glasses/tts", json={"text": "hi"})
        assert r.status_code == 503 and r.json()["detail"] == sr.NOT_CONNECTED_DECK


# ── agents no longer hold the deck's key ────────────────────────────


def test_agents_do_not_inherit_fds_soniox_key(monkeypatch):
    from captain_claw.flight_deck import server

    monkeypatch.setenv("SONIOX_API_KEY", KEY_ENV)
    assert "SONIOX_API_KEY" not in server._agent_base_env()
    env: dict[str, str] = {}
    server._share_fd_secrets(env)
    assert "SONIOX_API_KEY" not in env


# ── the agent-side client ───────────────────────────────────────────


class TestAgentClient:
    @pytest.fixture()
    def under_fd(self, monkeypatch, soniox):
        """The agent runs under a Flight Deck at http://fd.test (the mock)."""
        monkeypatch.delenv("SONIOX_API_KEY", raising=False)
        monkeypatch.setattr(soniox_client, "_fd_base", lambda: "http://fd.test")
        monkeypatch.setattr(soniox_client, "_fd_headers", lambda: {"X-Agent-Auth": "tok-bob"})
        monkeypatch.setattr(soniox_client, "_CONNECTED", (False, 0.0))
        return soniox

    async def test_transcribes_through_flight_deck(self, under_fd, monkeypatch):
        seen = []
        real = httpx.AsyncClient

        def fd(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/fd/soniox/agent/status":
                return httpx.Response(200, json={"connected": True})
            if request.url.path == "/fd/soniox/agent/transcribe":
                return httpx.Response(200, json={"text": "Hi there", "tokens": TOKENS[:1]})
            if request.url.path == "/fd/soniox/agent/realtime-key":
                return httpx.Response(200, json={"api_key": "temp:xyz", "expires_at": ""})
            return httpx.Response(404, json={"detail": "Not Found"})

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(fd)}))
        assert await soniox_client.available() is True
        out = await soniox_client.transcribe(b"wav", filename="r.wav", mime="audio/wav", language_hints=["hr"])
        assert out.text == "Hi there" and out.tokens == TOKENS[:1]
        req = seen[-1]
        assert req.content == b"wav" and req.headers["x-agent-auth"] == "tok-bob"
        assert req.url.params["language_hints"] == "hr" and req.url.params["filename"] == "r.wav"
        assert await soniox_client.realtime_key() == "temp:xyz"
        assert all(r.url.host == "fd.test" for r in seen)  # never Soniox directly

    async def test_an_old_flight_deck_says_so(self, under_fd, monkeypatch):
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{
            **kw, "transport": httpx.MockTransport(lambda r: httpx.Response(404, json={"detail": "Not Found"}))}))
        assert await soniox_client.available() is False
        with pytest.raises(soniox_api.SonioxError, match="needs an update"):
            await soniox_client.transcribe(b"wav")

    async def test_a_key_given_to_the_agent_is_used_directly(self, under_fd, monkeypatch):
        monkeypatch.setenv("SONIOX_API_KEY", KEY_ALICE)
        assert await soniox_client.available() is True
        out = await soniox_client.transcribe(b"wav", mime="audio/wav")
        assert out.text == "Hello world"
        assert {r["auth"] for r in under_fd.requests} == {f"Bearer {KEY_ALICE}"}
        assert await soniox_client.realtime_key() == KEY_ALICE

    async def test_standalone_without_a_key(self, monkeypatch):
        monkeypatch.delenv("SONIOX_API_KEY", raising=False)
        monkeypatch.setattr(soniox_client, "_fd_base", lambda: "")
        assert await soniox_client.available() is False
        with pytest.raises(soniox_api.SonioxError, match="isn't configured"):
            await soniox_client.transcribe(b"wav")
