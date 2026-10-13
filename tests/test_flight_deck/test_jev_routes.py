"""Jev (TypeSafe AI) on Flight Deck (``/fd/jev/*``, the ``jev`` flow tool and
the inbound MCP ``jev_ask``), through the real routes:

* the key is per FD user, verified before it is saved, and never returned —
  only a ``····ab12`` hint; ``jev:*`` user settings are server-owned (``/fd/settings``
  can't read or write them);
* an agent asks with its OWNER's key, the owner resolved from the deck's own
  records (unknown / missing identity and browsers → 403); an owner with no key
  is a 404 — never someone else's key;
* a user route never answers 401 for a Jev-side problem (the frontend reads 401
  as "your Flight Deck session expired");
* an auth-disabled deck keeps one key for the deck in ``system_settings``;
* a flow step asks with the run owner's key and returns JSON a branch can read;
  the MCP tool with the caller's.

Jev is never contacted: ``httpx.AsyncClient`` gets a mock transport.
"""

from __future__ import annotations

import json
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.jev_routes as jr
from captain_claw import jev_client
from captain_claw.flight_deck import agent_secret, auth, settings_routes
from captain_claw.flight_deck.db import FlightDeckDB

ALICE = "user-alice"
BOB = "user-bob"
KEY_ALICE = "jev_live_alice_0000aaaa"
KEY_BOB = "jev_live_bob_0000bbbb"
KEY_DECK = "jev_live_deck_0000dddd"
GOOD_KEYS = {KEY_ALICE, KEY_BOB, KEY_DECK}

MODELS = [{"name": "jev-latest", "description": "System One", "release_date": "2026-09-01"},
          {"name": "jev-1", "description": "first", "release_date": "2026-03-01"}]

QUESTIONS = [
    {"name": "urgent", "type": "noul", "instructions": "Does the sender need an answer today?"},
    {"name": "topic", "type": "choice", "instructions": "What is it mainly about?",
     "options": [{"name": "billing", "description": "invoices"}, {"name": "support"}]},
    {"name": "tone", "type": "score", "instructions": "How angry is the sender?",
     "levels": ["calm", "annoyed", "furious"]},
]


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
async def db(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    for var in ("FD_LOCKDOWN", "FD_AGENT_SHARED_SECRET"):
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
    try:
        yield d
    finally:
        agent_secret.reset_cache_for_tests()
        auth.set_auth_db(prev_db)
        await d.close()


@pytest.fixture()
def jev(monkeypatch):
    """A fake Jev behind every ``httpx.AsyncClient``: records each request;
    ``state.fail`` forces an outcome ("network", or an HTTP status)."""
    state = types.SimpleNamespace(requests=[], fail=None, retry_after=None)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        state.requests.append({
            "method": request.method, "path": request.url.path, "body": body,
            "auth": request.headers.get("authorization", ""),
        })
        if state.fail == "network":
            raise httpx.ConnectError("boom", request=request)
        if isinstance(state.fail, int):
            headers = {"retry-after": str(state.retry_after)} if state.retry_after else {}
            return httpx.Response(state.fail, json={"detail": "nope"}, headers=headers)
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in GOOD_KEYS:
            return httpx.Response(401, json={"detail": "invalid api key"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"models": MODELS})
        answers = {}
        for name, q in body["questions"].items():
            if q["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.91}
            elif q["type"] == "choice":
                first = next(iter(q["criteria"]))
                answers[name] = {"type": "choice", "choice": first, "confidence": 0.8,
                                 "probabilities": {k: (0.8 if k == first else 0.2) for k in q["criteria"]}}
            else:
                legend = {str(i): lvl for i, lvl in enumerate(q["criteria"])}
                answers[name] = {"type": "score", "score": 1.2, "legend": legend,
                                 "confidence": 0.7, "probabilities": {}}
        return httpx.Response(200, json={
            "model": body["model"], "answers": answers,
            "usage": {"input_tokens": 321, "output_tokens": 7},
        })

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        kw.setdefault("transport", httpx.MockTransport(handler))
        return real_client(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    # No retry back-off in tests (a 429's Retry-After would otherwise sleep).
    monkeypatch.setattr(jr, "_api", lambda key: jev_client.JevAPI(key, max_retries=0))
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
    app.include_router(jr.router)
    app.include_router(settings_routes.router)
    return TestClient(app, client=("127.0.0.1", 50123))


def _bearer(uid, role="user"):
    return {"Authorization": f"Bearer {auth.create_access_token(uid, role)}"}


async def _save(owner, key, **meta):
    await jr._write(owner, {jr._K_KEY: key, jr._K_META: json.dumps(
        {"verified": True, "verified_at": "2026-10-01T00:00:00+00:00",
         "updated_at": "2026-10-01T00:00:00+00:00", "models": ["jev-latest"], **meta})})


def _asks(jev):
    return [r for r in jev.requests if r["path"] == "/v1/systemone"]


async def _jev_calls(db, uid):
    return [json.loads(r["detail"]) for r in await db.get_usage_logs(user_id=uid, event_type="jev_call")]


# ── the key ─────────────────────────────────────────────────────────


class TestKey:
    async def test_status_without_a_key(self, db):
        r = _client().get("/fd/jev/status", headers=_bearer(BOB))
        assert r.status_code == 200
        assert r.json() == {
            "key_set": False, "key_hint": "", "verified": None, "verify_error": None,
            "verified_at": None,
            "updated_at": None, "models": [], "default_model": "jev-latest",
            "auth_enabled": True, "scope": "user",
        }

    async def test_save_verifies_and_status_never_returns_the_key(self, db, jev):
        c = _client()
        r = c.put("/fd/jev/key", headers=_bearer(BOB), json={"api_key": f"  {KEY_BOB}  "})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["key_set"] is True and body["verified"] is True
        assert body["key_hint"] == "····bbbb" and body["models"] == ["jev-latest", "jev-1"]
        assert body["verified_at"] and body["updated_at"] and "warning" not in body
        assert jev.requests[-1]["path"] == "/v1/models"
        assert jev.requests[-1]["auth"] == f"Bearer {KEY_BOB}"
        assert KEY_BOB not in r.text
        status = c.get("/fd/jev/status", headers=_bearer(BOB))
        assert status.json()["key_set"] is True and KEY_BOB not in status.text
        assert await jr.get_key(BOB) == KEY_BOB  # stripped, per user
        assert await jr.get_key(ALICE) == ""
        assert c.get("/fd/jev/status", headers=_bearer(ALICE)).json()["key_set"] is False

    async def test_a_key_jev_refuses_is_a_400_and_nothing_is_saved(self, db, jev):
        r = _client().put("/fd/jev/key", headers=_bearer(BOB), json={"api_key": "jev_wrong_key_123"})
        assert r.status_code == 400
        assert r.json()["detail"].startswith("Jev refused this key")
        assert "jev_wrong_key_123" not in r.text
        assert await jr.get_key(BOB) == ""
        assert await db.get_setting(BOB, jr._K_META) is None

    async def test_a_forbidden_key_is_a_400_too(self, db, jev):
        jev.fail = 403
        r = _client().put("/fd/jev/key", headers=_bearer(BOB), json={"api_key": KEY_BOB})
        assert r.status_code == 400 and await jr.get_key(BOB) == ""

    @pytest.mark.parametrize("fail", ["network", 503, 429])
    async def test_unverifiable_key_is_saved_with_a_warning(self, db, jev, fail):
        jev.fail = fail
        r = _client().put("/fd/jev/key", headers=_bearer(BOB), json={"api_key": KEY_BOB})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["key_set"] is True and body["verified"] is False and body["verified_at"] is None
        assert body["verify_error"] == "unreachable"
        assert body["warning"].startswith("Saved, but couldn't verify it with Jev: ")
        assert KEY_BOB not in r.text
        assert await jr.get_key(BOB) == KEY_BOB

    async def test_blank_key_is_a_422(self, db, jev):
        r = _client().put("/fd/jev/key", headers=_bearer(BOB), json={"api_key": "   "})
        assert r.status_code == 422 and r.json()["detail"] == "Paste a Jev API key"
        assert jev.requests == []

    @pytest.mark.parametrize("kwargs", [
        {"data": {"api_key": KEY_BOB}},                      # form-encoded (curl -d)
        {"content": json.dumps(KEY_BOB), "headers": {"Content-Type": "application/json"}},
        {"content": KEY_BOB, "headers": {"Content-Type": "text/plain"}},
        {"json": {"api_key": ["not", KEY_BOB]}},
    ])
    async def test_a_malformed_body_is_a_422_that_never_echoes_the_key(self, db, jev, kwargs, caplog):
        headers = {**_bearer(BOB), **kwargs.pop("headers", {})}
        r = _client().put("/fd/jev/key", headers=headers, **kwargs)
        assert r.status_code == 422 and r.json()["detail"] == "Paste a Jev API key"
        assert KEY_BOB not in r.text and KEY_BOB not in caplog.text
        assert jev.requests == [] and await jr.get_key(BOB) == ""

    @pytest.mark.parametrize("key", [
        KEY_BOB + "\u200b", "\u201c" + KEY_BOB + "\u201d", "jev_live bob", "jev_live_\u00e9",
    ])
    async def test_a_key_with_impossible_characters_is_refused_before_jev(self, db, jev, key):
        r = _client().put("/fd/jev/key", headers=_bearer(BOB), json={"api_key": key})
        assert r.status_code == 422 and "Copy it again" in r.json()["detail"]
        assert jev.requests == [] and await jr.get_key(BOB) == ""

    async def test_delete_forgets_key_and_meta(self, db, jev):
        await _save(BOB, KEY_BOB)
        await _save(ALICE, KEY_ALICE)
        r = _client().delete("/fd/jev/key", headers=_bearer(BOB))
        assert r.status_code == 200
        assert r.json()["key_set"] is False and r.json()["models"] == []
        assert await db.get_setting(BOB, jr._K_KEY) is None
        assert await db.get_setting(BOB, jr._K_META) is None
        assert await jr.get_key(ALICE) == KEY_ALICE  # only this user's

    async def test_test_lists_models_and_marks_the_key_verified(self, db, jev):
        await _save(BOB, KEY_BOB, verified=False, verified_at=None, models=[])
        r = _client().post("/fd/jev/test", headers=_bearer(BOB))
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["models"] == MODELS and isinstance(body["latency_ms"], int)
        meta = json.loads(await db.get_setting(BOB, jr._K_META))
        assert meta["verified"] is True and meta["verified_at"] and meta["models"] == ["jev-latest", "jev-1"]

    async def test_test_without_a_key_is_404_and_a_revoked_key_400(self, db, jev):
        c = _client()
        r = c.post("/fd/jev/test", headers=_bearer(BOB))
        assert r.status_code == 404 and r.json()["detail"] == jev_client.NOT_CONNECTED
        await _save(BOB, "jev_revoked_key_99")
        r = c.post("/fd/jev/test", headers=_bearer(BOB))
        assert r.status_code == 400  # never 401: that reads as an expired FD session
        assert json.loads(await db.get_setting(BOB, jr._K_META))["verified"] is False
        status = c.get("/fd/jev/status", headers=_bearer(BOB)).json()
        assert status["verified"] is False and status["verify_error"] == "refused"


class TestSettingsRoute:
    async def test_jev_settings_are_server_owned(self, db):
        await _save(BOB, KEY_BOB)
        await db.set_settings(BOB, {"theme": "dark"})
        c = _client()
        got = c.get("/fd/settings", headers=_bearer(BOB))
        assert got.status_code == 200 and got.json() == {"theme": "dark"}
        assert KEY_BOB not in got.text
        r = c.put("/fd/settings", headers=_bearer(BOB),
                  json={"settings": {"jev:api_key": "forged", "theme": "light"}})
        assert r.status_code == 400 and "jev:api_key" in r.json()["detail"]
        for key in ("jev:api_key", "JEV:meta"):
            assert c.delete(f"/fd/settings/{key}", headers=_bearer(BOB)).status_code == 400
        assert await jr.get_key(BOB) == KEY_BOB
        assert settings_routes._server_owned("jev:anything")


# ── asking (the playground) ─────────────────────────────────────────


class TestAsk:
    async def test_answers_values_summary_with_the_users_key(self, db, jev):
        await _save(BOB, KEY_BOB)
        await _save(ALICE, KEY_ALICE)
        r = _client().post("/fd/jev/ask", headers=_bearer(BOB), json={
            "state": "Hi, my invoice is wrong and I need it fixed today!!", "questions": QUESTIONS,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"model", "answers", "usage", "values", "summary", "latency_ms"}
        assert body["model"] == "jev-latest"
        assert body["values"] == {"urgent": 0.91, "topic": "billing", "tone": 1.2}
        assert body["answers"]["topic"]["probabilities"] == {"billing": 0.8, "support": 0.2}
        assert body["usage"] == {"input_tokens": 321, "output_tokens": 7}
        assert "urgent [yes/no]: p(yes) = 0.91 → yes" in body["summary"]
        (sent,) = _asks(jev)
        assert sent["auth"] == f"Bearer {KEY_BOB}"
        assert sent["body"]["questions"]["topic"]["criteria"] == {"billing": "invoices", "support": None}
        assert sent["body"]["questions"]["tone"]["criteria"] == ["calm", "annoyed", "furious"]
        (logged,) = await _jev_calls(db, BOB)
        assert logged == {"source": "ui", "agent": "", "model": "jev-latest", "questions": 3,
                          "input_tokens": 321, "output_tokens": 7,
                          "latency_ms": logged["latency_ms"], "ok": True, "status": 200}

    async def test_json_text_state_and_question_map_and_model(self, db, jev):
        await _save(BOB, KEY_BOB)
        r = _client().post("/fd/jev/ask", headers=_bearer(BOB), json={
            "state": '{"subject": "refund", "body": "please"}',
            "questions": json.dumps({"refund": {"type": "boolean", "question": "Is it about a refund?"}}),
            "model": "jev-1",
        })
        assert r.status_code == 200, r.text
        (sent,) = _asks(jev)
        assert sent["body"] == {"model": "jev-1", "state": {"subject": "refund", "body": "please"},
                                "questions": {"refund": {"type": "noul",
                                                         "instructions": "Is it about a refund?"}}}

    @pytest.mark.parametrize("questions", [
        [], [{"name": "x", "type": "choice", "instructions": "Pick", "options": ["only-one"]}],
        [{"name": "x", "type": "essay", "instructions": "Write"}], "not json",
        [{"name": "x", "type": "score", "instructions": "Rate", "levels": ["one"]}],
    ])
    async def test_bad_questions_are_a_422_before_jev(self, db, jev, questions):
        await _save(BOB, KEY_BOB)
        r = _client().post("/fd/jev/ask", headers=_bearer(BOB),
                           json={"state": "text", "questions": questions})
        assert r.status_code == 422 and isinstance(r.json()["detail"], str)
        assert jev.requests == [] and await _jev_calls(db, BOB) == []

    async def test_empty_state_is_a_422(self, db, jev):
        await _save(BOB, KEY_BOB)
        r = _client().post("/fd/jev/ask", headers=_bearer(BOB), json={"state": "  ", "questions": QUESTIONS})
        assert r.status_code == 422 and "state" in r.json()["detail"]

    async def test_no_key_is_a_404(self, db, jev):
        await _save(ALICE, KEY_ALICE)
        r = _client().post("/fd/jev/ask", headers=_bearer(BOB), json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == 404 and r.json()["detail"] == jev_client.NOT_CONNECTED
        assert jev.requests == []

    async def test_upstream_401_is_a_400_never_a_401(self, db, jev):
        await _save(BOB, "jev_revoked_key_99")
        r = _client().post("/fd/jev/ask", headers=_bearer(BOB), json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == 400 and "401" in r.json()["detail"]
        meta = json.loads(await db.get_setting(BOB, jr._K_META))
        assert meta["verified"] is False and meta["verify_error"] == "refused"
        (logged,) = await _jev_calls(db, BOB)
        assert logged["ok"] is False and logged["status"] == 401

    async def test_a_2xx_without_answers_is_a_502_logged_as_failed(self, db, jev, monkeypatch):
        await _save(BOB, KEY_BOB)

        class _NoAnswers(jev_client.JevAPI):
            async def _send(self, method, path, body):
                return {"model": "jev-latest"}

        monkeypatch.setattr(jr, "_api", lambda key: _NoAnswers(key, max_retries=0))
        r = _client().post("/fd/jev/ask", headers=_bearer(BOB), json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == 502
        (logged,) = await _jev_calls(db, BOB)
        assert logged["ok"] is False and logged["status"] == 200

    @pytest.mark.parametrize("upstream,expected", [
        (403, 400), (404, 400), (422, 422), (429, 429), (500, 502), (503, 502), (529, 502),
        ("network", 502),
    ])
    async def test_error_mapping(self, db, jev, upstream, expected):
        await _save(BOB, KEY_BOB)
        jev.fail = upstream
        if upstream == 429:
            jev.retry_after = 7
        r = _client().post("/fd/jev/ask", headers=_bearer(BOB), json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == expected, r.text
        assert r.status_code != 401
        if upstream == 429:
            assert r.headers["retry-after"] == "7"

    async def test_a_verified_flag_heals_on_success(self, db, jev):
        await _save(BOB, KEY_BOB, verified=False, verified_at=None, verify_error="refused")
        assert _client().post("/fd/jev/ask", headers=_bearer(BOB),
                              json={"state": "x", "questions": QUESTIONS}).status_code == 200
        meta = json.loads(await db.get_setting(BOB, jr._K_META))
        assert meta["verified"] is True and meta["verified_at"] and meta["verify_error"] is None

    async def test_user_routes_need_a_session(self, db, jev):
        c = _client()
        assert c.get("/fd/jev/status").status_code == 401
        assert c.post("/fd/jev/ask", json={"state": "x", "questions": QUESTIONS}).status_code == 401


# ── agents ──────────────────────────────────────────────────────────


class TestAgent:
    async def test_agent_asks_with_its_owners_key(self, db, jev, agents):
        await _save(ALICE, KEY_ALICE)
        await _save(BOB, KEY_BOB)
        body = jev_client.build_request("state text", QUESTIONS)
        r = _client().post("/fd/jev/agent/ask", headers={"X-Agent-Auth": "tok-bob"}, json=body)
        assert r.status_code == 200, r.text
        assert r.json()["values"]["urgent"] == 0.91
        assert KEY_BOB not in r.text
        (sent,) = _asks(jev)
        assert sent["auth"] == f"Bearer {KEY_BOB}"
        (logged,) = await _jev_calls(db, BOB)
        assert logged["source"] == "agent" and logged["agent"] == "bob-bot"

    async def test_owner_without_a_key_is_404_never_someone_elses(self, db, jev, agents):
        await _save(ALICE, KEY_ALICE)
        c = _client()
        r = c.post("/fd/jev/agent/ask", headers={"X-Agent-Auth": "tok-bob"},
                   json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == 404 and r.json()["detail"] == jev_client.NOT_CONNECTED
        assert jev.requests == []
        assert c.get("/fd/jev/agent/models", headers={"X-Agent-Auth": "tok-bob"}).status_code == 404

    async def test_unknown_or_missing_identity_is_refused(self, db, jev, agents):
        await _save(ALICE, KEY_ALICE)
        c = _client()
        for headers in ({}, {"X-Agent-Auth": "other-deck"}):
            r = c.post("/fd/jev/agent/ask", headers=headers, json={"state": "x", "questions": QUESTIONS})
            assert r.status_code == 403, r.text
            assert c.get("/fd/jev/agent/status", headers=headers).status_code == 403
        assert jev.requests == []
        assert "Jev connections" in c.get("/fd/jev/agent/status").json()["detail"]

    @pytest.mark.parametrize("browser", [{"Origin": "http://localhost:25080"},
                                         {"Sec-Fetch-Mode": "cors"}, {"Sec-Fetch-Site": "same-origin"}])
    async def test_browsers_are_refused(self, db, jev, agents, browser):
        await _save(BOB, KEY_BOB)
        c = _client()
        headers = {"X-Agent-Auth": "tok-bob", **browser}
        r = c.post("/fd/jev/agent/ask", headers=headers, json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == 403 and jev.requests == []
        assert c.get("/fd/jev/agent/status", headers=headers).status_code == 403
        assert c.get("/fd/jev/agent/models", headers=headers).status_code == 403

    async def test_remote_caller_without_the_agent_secret_is_refused(self, db, jev, agents):
        await _save(BOB, KEY_BOB)
        app = FastAPI()
        app.include_router(jr.router)
        remote = TestClient(app, client=("10.1.2.3", 50123))
        r = remote.get("/fd/jev/agent/status", headers={"X-Agent-Auth": "tok-bob"})
        assert r.status_code == 401 and jev.requests == []

    async def test_agent_status(self, db, jev, agents):
        await _save(BOB, KEY_BOB)
        c = _client()
        assert c.get("/fd/jev/agent/status", headers={"X-Agent-Auth": "tok-bob"}).json() == {"connected": True}
        assert c.get("/fd/jev/agent/status", headers={"X-Agent-Auth": "tok-alice"}).json() == {"connected": False}
        assert jev.requests == []  # no network

    async def test_agent_models(self, db, jev, agents):
        await _save(BOB, KEY_BOB)
        r = _client().get("/fd/jev/agent/models", headers={"X-Agent-Auth": "tok-bob"})
        assert r.status_code == 200 and r.json() == {"models": MODELS}
        assert jev.requests[-1]["auth"] == f"Bearer {KEY_BOB}"

    async def test_agent_upstream_401_is_a_400(self, db, jev, agents):
        await _save(BOB, "jev_revoked_key_99")
        r = _client().post("/fd/jev/agent/ask", headers={"X-Agent-Auth": "tok-bob"},
                           json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == 400

    async def test_ex_users_agent_is_refused(self, db, jev, agents):
        agents["tok-gone"] = ("user-gone", "gone-bot")
        await _save(ALICE, KEY_ALICE)
        r = _client().get("/fd/jev/agent/status", headers={"X-Agent-Auth": "tok-gone"})
        assert r.status_code == 403


# ── auth-disabled deck ──────────────────────────────────────────────


class TestAuthOffDeck:
    @pytest.fixture()
    def off(self, db, monkeypatch):
        monkeypatch.setenv("FD_AUTH_ENABLED", "false")
        return db

    async def test_one_key_for_the_deck_in_system_settings(self, off, jev):
        c = _client()
        r = c.put("/fd/jev/key", json={"api_key": KEY_DECK})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scope"] == "deck" and body["auth_enabled"] is False and body["verified"] is True
        assert KEY_DECK not in r.text
        assert await off.get_system_setting(jr._K_KEY) == KEY_DECK
        assert await off.get_setting("local", jr._K_KEY) is None
        r = c.post("/fd/jev/ask", json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == 200 and _asks(jev)[0]["auth"] == f"Bearer {KEY_DECK}"
        (logged,) = await _jev_calls(off, "local")
        assert logged["source"] == "ui"
        r = c.delete("/fd/jev/key")
        assert r.status_code == 200 and r.json()["key_set"] is False
        assert await jr.get_key("local") == "" and await jr.get_key("") == ""
        assert c.get("/fd/jev/status").json()["key_set"] is False

    async def test_agents_use_the_deck_key(self, off, jev, agents):
        await _save("local", KEY_DECK)
        c = _client()
        assert c.get("/fd/jev/agent/status", headers={"X-Agent-Auth": "tok-local"}).json() == {"connected": True}
        r = c.post("/fd/jev/agent/ask", headers={"X-Agent-Auth": "tok-local"},
                   json={"state": "x", "questions": QUESTIONS})
        assert r.status_code == 200 and _asks(jev)[0]["auth"] == f"Bearer {KEY_DECK}"
        (logged,) = await _jev_calls(off, "local")
        assert logged["agent"] == "desk-bot"

    async def test_agent_gate_still_applies(self, off, jev, agents):
        await _save("local", KEY_DECK)
        c = _client()
        assert c.get("/fd/jev/agent/status").status_code == 403
        assert c.get("/fd/jev/agent/status", headers={"X-Agent-Auth": "nope"}).status_code == 403
        assert c.get("/fd/jev/agent/status",
                     headers={"X-Agent-Auth": "tok-local", "Origin": "http://evil.example"}).status_code == 403


# ── flows ───────────────────────────────────────────────────────────


class TestFlowTool:
    def _jev_tool(self):
        import captain_claw.flight_deck.server as srv

        return srv._fd_internal_tools()["jev"]

    async def test_ok_with_values(self, db, jev):
        await _save(BOB, KEY_BOB)
        out = json.loads(await self._jev_tool()(
            {"state": "invoice wrong!!", "questions": json.dumps(QUESTIONS)}, owner_id=BOB))
        assert out["ok"] is True
        assert set(out) == {"ok", "model", "answers", "values", "usage", "summary"}
        assert out["values"] == {"urgent": 0.91, "topic": "billing", "tone": 1.2}
        assert _asks(jev)[0]["auth"] == f"Bearer {KEY_BOB}"
        (logged,) = await _jev_calls(db, BOB)
        assert logged["source"] == "flow"

    @pytest.mark.parametrize("args,owner,needle", [
        ({"state": "x", "questions": json.dumps(QUESTIONS)}, ALICE, "isn't connected"),
        ({"state": "x", "questions": "[{"}, BOB, "not valid JSON"),
        ({"state": "", "questions": json.dumps(QUESTIONS)}, BOB, "state"),
        ({"state": "x", "questions": json.dumps(QUESTIONS)}, "", "no owner"),
    ])
    async def test_errors_are_ok_false_never_raised(self, db, jev, args, owner, needle):
        await _save(BOB, KEY_BOB)
        out = json.loads(await self._jev_tool()(args, owner_id=owner))
        assert out["ok"] is False and needle in out["error"]
        assert _asks(jev) == []

    async def test_jev_failure_is_ok_false(self, db, jev):
        await _save(BOB, KEY_BOB)
        jev.fail = 503
        out = json.loads(await self._jev_tool()({"state": "x", "questions": json.dumps(QUESTIONS)},
                                                 owner_id=BOB))
        assert out == {"ok": False, "error": out["error"]} and "503" in out["error"]

    async def test_auth_off_uses_the_deck_key_whatever_the_owner(self, db, jev, monkeypatch):
        monkeypatch.setenv("FD_AUTH_ENABLED", "false")
        await _save("local", KEY_DECK)
        out = json.loads(await self._jev_tool()({"state": "x", "questions": json.dumps(QUESTIONS)}))
        assert out["ok"] is True and _asks(jev)[0]["auth"] == f"Bearer {KEY_DECK}"

    async def test_face_identify_accepts_the_owner_kwarg(self):
        import captain_claw.flight_deck.server as srv

        out = json.loads(await srv._fd_internal_tools()["face_identify"]({}, owner_id=BOB))
        assert out["confident"] is False and "no image" in out["error"]


def _runner(fd_tools, *, enforce_owner=True):
    from captain_claw.flight_deck.flow_runner import FlowRunner

    return FlowRunner(
        types.SimpleNamespace(), get_agents=lambda: [], resolve_auth=lambda port: "",
        fd_self_base="http://fd.invalid", fd_tools=fd_tools, enforce_owner=enforce_owner,
    )


class TestFlowRunner:
    async def test_runner_passes_the_runs_owner(self):
        seen = []

        async def fake(args, **kw):
            seen.append((args, kw))
            return json.dumps({"ok": True, "values": {"urgent": 0.9}})

        flow = {
            "id": "f1", "name": "t", "owner_id": BOB,
            "trigger": {"on": "message", "channel": "any", "match": {"kind": "always"}},
            "steps": [{"id": "j", "type": "tool", "tool": "jev", "on": "fd",
                       "args": {"state": "{{trigger.text}}", "questions": "[]"}}],
            "output": {"channel": "log"},
        }
        res = await _runner({"jev": fake}).run(flow, {"text": "hello"}, dry=True)
        assert res["status"] == "done", res
        assert seen == [({"state": "hello", "questions": "[]"}, {"owner_id": BOB})]
        # A caller's context (a route's user) wins over the flow's owner.
        await _runner({"jev": fake}, enforce_owner=False).run(flow, {"text": "hi"}, dry=True,
                                                                owner_id=ALICE)
        assert seen[-1][1] == {"owner_id": ALICE}

    async def test_dsl_flow_branches_on_jev_values_with_the_owners_key(self, db, jev):
        import captain_claw.flight_deck.server as srv
        from captain_claw.flight_deck import flow_dsl

        await _save(ALICE, KEY_ALICE)
        await _save(BOB, KEY_BOB)
        questions = json.dumps(QUESTIONS[:2], separators=(",", ":"))
        flow = flow_dsl.compile_dsl(f'''flow "Triage"
trigger any when contains "invoice"

step triage:
  tool on fd
  tool: jev
  arg state: {{{{trigger.text}}}}
  arg questions: {questions}

step route:
  branch
  if not {{{{steps.triage.ok}}}} -> failed
  if {{{{steps.triage.values.urgent}}}} >= 0.7 -> hot
  else -> cold

step failed:
  return failed: {{{{steps.triage.error}}}}

step hot:
  return hot {{{{steps.triage.values.topic}}}}

step cold:
  return cold

output -> return
''')
        flow.update(id="f2", owner_id=BOB)
        res = await _runner(srv._fd_internal_tools()).run(flow, {"text": "my invoice is wrong"}, dry=True)
        assert res["status"] == "done", res
        assert res["output"] == "hot billing"
        (sent,) = _asks(jev)
        assert sent["auth"] == f"Bearer {KEY_BOB}" and sent["body"]["state"] == "my invoice is wrong"

        # The same flow owned by a user without a key takes the failure path.
        await jr._erase(BOB)
        res = await _runner(srv._fd_internal_tools()).run(flow, {"text": "invoice"}, dry=True)
        assert res["output"].startswith("failed: Jev isn't connected")
        assert len(_asks(jev)) == 1


# ── inbound MCP ─────────────────────────────────────────────────────


class TestMcp:
    def _mcp(self):
        from captain_claw.flight_deck import mcp_server_routes as mcp

        app = FastAPI()
        app.include_router(mcp.router)
        return TestClient(app, client=("127.0.0.1", 50123))

    async def _pat(self, db, uid):
        raw = auth.new_pat()
        await db.create_pat(uid, auth.hash_token(raw), "test")
        return {"Authorization": f"Bearer {raw}"}

    def _call(self, c, headers, name, arguments, rid=1):
        r = c.post("/fd/mcp-server", headers=headers, json={
            "jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        assert r.status_code == 200, r.text
        return r.json()["result"]

    async def test_listed(self, db):
        c = self._mcp()
        r = c.post("/fd/mcp-server", headers=await self._pat(db, BOB),
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool = next(t for t in r.json()["result"]["tools"] if t["name"] == "jev_ask")
        schema = tool["inputSchema"]
        assert schema["required"] == ["state", "questions"]
        assert "type" not in schema["properties"]["state"]
        assert schema["properties"]["model"]["type"] == "string"

    async def test_asks_with_the_callers_key(self, db, jev):
        await _save(ALICE, KEY_ALICE)
        await _save(BOB, KEY_BOB)
        c = self._mcp()
        result = self._call(c, await self._pat(db, BOB), "jev_ask",
                            {"state": {"subject": "invoice"}, "questions": QUESTIONS})
        assert result["isError"] is False
        data = json.loads(result["content"][0]["text"])
        assert data["values"]["topic"] == "billing" and data["summary"]
        (sent,) = _asks(jev)
        assert sent["auth"] == f"Bearer {KEY_BOB}" and sent["body"]["state"] == {"subject": "invoice"}
        (logged,) = await _jev_calls(db, BOB)
        assert logged["source"] == "mcp"

    async def test_errors_are_in_band(self, db, jev):
        c = self._mcp()
        headers = await self._pat(db, BOB)
        result = self._call(c, headers, "jev_ask", {"state": "x", "questions": QUESTIONS})
        assert result["isError"] is True and "isn't connected" in result["content"][0]["text"]
        await _save(BOB, KEY_BOB)
        result = self._call(c, headers, "jev_ask", {"state": "x", "questions": []})
        assert result["isError"] is True and "at least one question" in result["content"][0]["text"]
        assert jev.requests == []
