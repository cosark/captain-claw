"""The deck's shared Telegram bot (telegram_shared.py + telegram_shared_routes.py).

* Flight Deck routes each update by the sender's Telegram id: strangers and
  pending / rejected users get a (rate-limited) notice, an approved and linked
  user's update waits in their agent's queue — never anyone else's;
* ``/start <code>`` from the Connections link binds a Telegram account to the
  user who asked, once, and never to a second Flight Deck user;
* the relay serves the agent's ``getUpdates`` from its queue and forwards other
  Bot API calls with the shared token only inside the agent's own chats, files
  and button presses; bot-wide settings land on the agent's chats instead;
* users ask with their OWN agents, admins approve; the token never leaves FD.

Telegram is never contacted: ``telegram_shared._http`` gets a mock transport.
"""

from __future__ import annotations

import asyncio
import json
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.telegram_shared as ts
import captain_claw.flight_deck.telegram_shared_routes as tsr
from captain_claw.flight_deck import agent_secret, auth
from captain_claw.flight_deck.db import FlightDeckDB

ALICE = "user-alice"   # admin
BOB = "user-bob"
CAROL = "user-carol"
BOT_TOKEN = "123456:SHARED-bot-token-xyz"
TG_BOB = 5001
TG_CAROL = 5002
TG_STRANGER = 5999


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
    for uid, email, role in [(ALICE, "alice@x.co", "admin"), (BOB, "bob@x.co", "user"),
                             (CAROL, "carol@x.co", "user")]:
        await d._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, email, "h", uid.split("-")[1].title(), role, now, now),
        )
    await d._db.commit()
    monkeypatch.setattr(ts, "_state", ts._State())
    try:
        yield d
    finally:
        agent_secret.reset_cache_for_tests()
        auth.set_auth_db(prev_db)
        await d.close()


@pytest.fixture()
def tg(monkeypatch):
    """A fake Telegram behind ``telegram_shared._http``; records every call."""
    state = types.SimpleNamespace(calls=[])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/file/"):
            state.calls.append({"method": "FILE", "path": path, "body": None})
            return httpx.Response(200, content=b"PHOTO", headers={"content-type": "image/jpeg"})
        _, bot, method = path.split("/", 2)
        ctype = request.headers.get("content-type", "")
        body = json.loads(request.content) if "json" in ctype and request.content else None
        state.calls.append({"method": method, "token": bot[3:], "body": body,
                            "raw": request.content, "ctype": ctype})
        if bot[3:] != BOT_TOKEN:
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 42, "is_bot": True, "first_name": "Team", "username": "team_bot"}})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": body["file_id"], "file_path": "photos/file_1.jpg"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handler)
    # A fresh client per call: the app (TestClient) and the tests run different loops.
    monkeypatch.setattr(ts, "_http", lambda: httpx.AsyncClient(transport=transport))

    async def idle_poll(db, token):  # never poll in tests
        await asyncio.Event().wait()

    monkeypatch.setattr(ts, "_poll_loop", idle_poll)
    return state


@pytest.fixture()
def agents(monkeypatch):
    """This deck's agents: web_auth token → (owner, slug); and each user's own."""
    import captain_claw.flight_deck.server as srv

    registry = {"tok-bob": (BOB, "bob-bot"), "tok-bob2": (BOB, "bob-second"),
                "tok-carol": (CAROL, "carol-bot")}

    def find(token):
        if token in registry:
            owner, slug = registry[token]
            return True, owner, slug
        return False, "", ""

    monkeypatch.setattr(srv, "_find_agent_by_auth", find)

    def own(user_id):
        return [{"slug": slug, "name": slug.replace("-", " ").title(), "kind": "process",
                 "status": "running"} for owner, slug in registry.values() if owner == user_id]

    monkeypatch.setattr(tsr, "own_agents", own)
    return registry


def _client():
    app = FastAPI()
    app.include_router(tsr.router)
    return TestClient(app, client=("127.0.0.1", 50123))


def _bearer(uid, role="user"):
    return {"Authorization": f"Bearer {auth.create_access_token(uid, role)}"}


def _msg(update_id, tg_user, text="hi", chat_type="private", **extra):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "text": text,
        "from": {"id": tg_user, "is_bot": False, "first_name": "Bo", "username": f"u{tg_user}"},
        "chat": {"id": tg_user, "type": chat_type}, **extra}}


async def _bot_on(db):
    await ts.save_config(db, {"token": BOT_TOKEN, "enabled": True, "bot_username": "team_bot"})
    await ts.start(db)


async def _link(db, user, slug, tg_user, status="approved"):
    await db.create_telegram_link(user, slug, slug.title(), ts.new_link_code())
    await db.update_telegram_link(user, status=status, tg_user_id=str(tg_user), link_code="")
    await ts.reload_links(db)


def _sent(tg, method="sendMessage"):
    return [c for c in tg.calls if c["method"] == method]


# ── routing ─────────────────────────────────────────────────────────


class TestRouting:
    async def test_a_stranger_gets_one_notice_and_nothing_is_queued(self, db, tg):
        await _bot_on(db)
        assert await ts.route_update(db, _msg(1, TG_STRANGER)) == "unknown"
        assert await ts.route_update(db, _msg(2, TG_STRANGER)) == "unknown"
        (notice,) = _sent(tg)
        assert notice["body"]["chat_id"] == TG_STRANGER and "Connections" in notice["body"]["text"]
        assert all(not b.queue for b in ts._state.boxes.values())

    async def test_start_code_links_the_account_to_the_requester(self, db, tg):
        await _bot_on(db)
        row = await db.create_telegram_link(BOB, "bob-bot", "Bob Bot", "code-bob-1")
        await ts.reload_links(db)
        assert await ts.route_update(db, _msg(1, TG_BOB, "/start code-bob-1")) == "linked"
        link = await db.get_telegram_link(BOB)
        assert link["tg_user_id"] == str(TG_BOB) and link["tg_username"] == f"u{TG_BOB}"
        assert link["link_code"] == "" and row["status"] == "pending"
        assert "admin" in _sent(tg)[-1]["body"]["text"]
        # Pending: a notice, not the agent.
        assert await ts.route_update(db, _msg(2, TG_BOB)) == "pending"
        # The code worked once.
        assert await ts.route_update(db, _msg(3, TG_CAROL, "/start code-bob-1")) == "unknown"

    async def test_an_account_linked_to_one_user_cant_be_claimed_by_another(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await db.create_telegram_link(CAROL, "carol-bot", "Carol Bot", "code-carol")
        await ts.reload_links(db)
        assert await ts.route_update(db, _msg(1, TG_BOB, "/start code-carol")) == "linked"
        assert (await db.get_telegram_link(CAROL))["tg_user_id"] == ""
        assert (await db.get_telegram_link(BOB))["tg_user_id"] == str(TG_BOB)
        assert "another Flight Deck user" in _sent(tg)[-1]["body"]["text"]

    async def test_approved_messages_reach_only_their_own_agent(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await _link(db, CAROL, "carol-bot", TG_CAROL)
        assert await ts.route_update(db, _msg(10, TG_BOB, "for bob")) == "queued"
        assert await ts.route_update(db, _msg(11, TG_CAROL, "for carol")) == "queued"
        got = await ts.relay_get_updates("bob-bot", BOB, None, 0)
        assert [u["message"]["text"] for u in got] == ["for bob"]
        # The right slug with the wrong owner gets nothing.
        assert await ts.relay_get_updates("bob-bot", CAROL, None, 0) == []
        # Confirming with offset drops it, like Telegram.
        assert await ts.relay_get_updates("bob-bot", BOB, 11, 0) == []

    async def test_groups_and_rejected_users_never_reach_an_agent(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await _link(db, CAROL, "carol-bot", TG_CAROL, status="rejected")
        assert await ts.route_update(db, _msg(1, TG_BOB, chat_type="group")) == "ignored:group"
        assert await ts.route_update(db, _msg(2, TG_CAROL)) == "rejected"
        assert await ts.relay_get_updates("bob-bot", BOB, None, 0) == []
        assert await ts.relay_get_updates("carol-bot", CAROL, None, 0) == []

    async def test_a_held_poll_wakes_when_a_message_arrives(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        poll = asyncio.create_task(ts.relay_get_updates("bob-bot", BOB, None, 10))
        await asyncio.sleep(0.05)
        assert not poll.done()
        await ts.route_update(db, _msg(7, TG_BOB, "wake"))
        got = await asyncio.wait_for(poll, 2)
        assert got[0]["message"]["text"] == "wake"

    async def test_removing_access_drops_what_was_waiting(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _msg(1, TG_BOB))
        await db.delete_telegram_link(BOB)
        await ts.reload_links(db)
        assert not ts._box("bob-bot").queue

    async def test_offline_agent_notice(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _msg(1, TG_BOB))
        assert "isn't running" in _sent(tg)[-1]["body"]["text"]


class TestCheckCall:
    async def test_only_the_agents_own_chats_files_and_buttons(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await _link(db, CAROL, "carol-bot", TG_CAROL)
        update = _msg(1, TG_BOB, photo=[{"file_id": "small"}, {"file_id": "big"}])
        await ts.route_update(db, update)
        await ts.route_update(db, {"update_id": 2, "callback_query": {
            "id": "cb-1", "data": "ns:0", "from": {"id": TG_BOB},
            "message": {"message_id": 1, "chat": {"id": TG_BOB, "type": "private"}}}})

        ts.check_call("bob-bot", BOB, "sendMessage", {"chat_id": TG_BOB, "text": "x"})
        ts.check_call("bob-bot", BOB, "sendPhoto", {"chat_id": str(TG_BOB)})
        ts.check_call("bob-bot", BOB, "getFile", {"file_id": "big"})
        ts.check_call("bob-bot", BOB, "answerCallbackQuery", {"callback_query_id": "cb-1"})
        for method, params in [
            ("sendMessage", {"chat_id": TG_CAROL, "text": "x"}),
            ("sendMessage", {"text": "no chat"}),
            ("forwardMessage", {"chat_id": TG_BOB, "from_chat_id": TG_CAROL, "message_id": 1}),
            ("getFile", {"file_id": "someone-elses"}),
            ("answerCallbackQuery", {"callback_query_id": "cb-other"}),
            ("leaveChat", {"chat_id": TG_BOB}),
            ("setWebhook", {"url": "https://evil"}),
            ("readBusinessMessage", {"chat_id": TG_BOB}),
            ("editMessageText", {"chat_id": TG_BOB, "inline_message_id": "x", "text": "t"}),
            ("sendMessage", {"chat_id": TG_BOB, "business_connection_id": "b", "text": "t"}),
        ]:
            with pytest.raises(ts.RelayRefused):
                ts.check_call("bob-bot", BOB, method, params)
        # Carol's agent can't use Bob's chat, nor can Bob's chat be used under another owner.
        with pytest.raises(ts.RelayRefused):
            ts.check_call("carol-bot", CAROL, "sendMessage", {"chat_id": TG_BOB})
        with pytest.raises(ts.RelayRefused):
            ts.check_call("bob-bot", CAROL, "sendMessage", {"chat_id": TG_BOB})


# ── routes ──────────────────────────────────────────────────────────


class TestUserAndAdminRoutes:
    async def test_no_bot_yet(self, db, tg, agents):
        r = _client().get("/fd/telegram-shared", headers=_bearer(BOB))
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False and body["link"] is None
        assert [a["slug"] for a in body["agents"]] == ["bob-bot", "bob-second"]
        r = _client().post("/fd/telegram-shared/request", headers=_bearer(BOB),
                           json={"agent_slug": "bob-bot"})
        assert r.status_code == 409

    async def test_admin_sets_the_token_after_telegram_accepts_it(self, db, tg, agents):
        c = _client()
        r = c.put("/fd/admin/telegram-shared", headers=_bearer(ALICE, "admin"),
                  json={"token": "999:wrong"})
        assert r.status_code == 400 and "999:wrong" not in r.text
        r = c.put("/fd/admin/telegram-shared", headers=_bearer(ALICE, "admin"),
                  json={"token": BOT_TOKEN})
        assert r.status_code == 200, r.text
        cfg = r.json()["config"]
        assert cfg["configured"] and cfg["bot_username"] == "team_bot"
        assert BOT_TOKEN not in r.text and cfg["token_hint"].endswith(BOT_TOKEN[-4:])
        assert ts.token() == BOT_TOKEN
        assert c.put("/fd/admin/telegram-shared", headers=_bearer(BOB),
                     json={"token": BOT_TOKEN}).status_code == 403
        assert c.get("/fd/admin/telegram-shared", headers=_bearer(BOB)).status_code == 403

    async def test_request_approve_switch_disconnect(self, db, tg, agents):
        await _bot_on(db)
        c = _client()
        # Someone else's agent: refused.
        r = c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "carol-bot"})
        assert r.status_code == 404
        r = c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-bot"})
        assert r.status_code == 200, r.text
        link = r.json()["link"]
        assert link["status"] == "pending" and link["agent_slug"] == "bob-bot"
        assert link["deep_link"].startswith("https://t.me/team_bot?start=")
        notes = await db.list_notifications(ALICE)
        assert notes and notes[0]["type"] == "telegram"

        # Bob opens the bot through his link.
        code = link["deep_link"].rsplit("=", 1)[1]
        await ts.route_update(db, _msg(1, TG_BOB, f"/start {code}"))

        admin = c.post(f"/fd/admin/telegram-shared/links/{BOB}/approve", headers=_bearer(ALICE, "admin"))
        assert admin.status_code == 200
        (row,) = admin.json()["links"]
        assert row["status"] == "approved" and row["linked"] and row["user_email"] == "bob@x.co"
        assert "Approved" in _sent(tg)[-1]["body"]["text"]
        assert await ts.route_update(db, _msg(2, TG_BOB)) == "queued"

        # Switching to another of his own agents keeps the approval.
        r = c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-second"})
        assert r.json()["link"]["status"] == "approved"
        assert r.json()["link"]["agent_slug"] == "bob-second"
        assert ts.chats_for_slug("bob-second", BOB) == {str(TG_BOB)}
        assert ts.chats_for_slug("bob-bot", BOB) == set()

        r = c.delete("/fd/telegram-shared", headers=_bearer(BOB))
        assert r.json()["link"] is None
        assert await ts.route_update(db, _msg(3, TG_BOB)) == "unknown"

    async def test_reject_then_ask_again(self, db, tg, agents):
        await _bot_on(db)
        c = _client()
        c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-bot"})
        c.post(f"/fd/admin/telegram-shared/links/{BOB}/reject", headers=_bearer(ALICE, "admin"))
        assert c.get("/fd/telegram-shared", headers=_bearer(BOB)).json()["link"]["status"] == "rejected"
        r = c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-bot"})
        assert r.json()["link"]["status"] == "pending"

    async def test_auto_approve_lets_new_requests_straight_in(self, db, tg, agents):
        await _bot_on(db)
        c = _client()
        r = c.put("/fd/admin/telegram-shared", headers=_bearer(ALICE, "admin"), json={"auto_approve": True})
        assert r.status_code == 200 and r.json()["config"]["auto_approve"] is True
        assert c.get("/fd/telegram-shared", headers=_bearer(BOB)).json()["auto_approve"] is True

        r = c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-bot"})
        link = r.json()["link"]
        assert link["status"] == "approved" and link["deep_link"].startswith("https://t.me/team_bot?start=")
        assert (await db.get_telegram_link(BOB))["decided_by"] == "auto"
        notes = await db.list_notifications(ALICE)
        assert notes and "automatically" in notes[0]["title"]

        # Opening the bot through the link connects at once: no admin step.
        await ts.route_update(db, _msg(1, TG_BOB, f"/start {link['deep_link'].rsplit('=', 1)[1]}"))
        assert "Connected" in _sent(tg)[-1]["body"]["text"]
        assert await ts.route_update(db, _msg(2, TG_BOB)) == "queued"

    async def test_auto_approve_leaves_turned_down_people_to_an_admin(self, db, tg, agents):
        await _bot_on(db)
        c = _client()
        c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-bot"})
        c.post(f"/fd/admin/telegram-shared/links/{BOB}/reject", headers=_bearer(ALICE, "admin"))
        c.put("/fd/admin/telegram-shared", headers=_bearer(ALICE, "admin"), json={"auto_approve": True})
        r = c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-bot"})
        assert r.json()["link"]["status"] == "pending"

    async def test_switching_auto_approve_on_approves_who_is_waiting(self, db, tg, agents):
        await _bot_on(db)
        c = _client()
        c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-bot"})
        c.post("/fd/telegram-shared/request", headers=_bearer(CAROL), json={"agent_slug": "carol-bot"})
        c.post(f"/fd/admin/telegram-shared/links/{CAROL}/reject", headers=_bearer(ALICE, "admin"))
        r = c.put("/fd/admin/telegram-shared", headers=_bearer(ALICE, "admin"), json={"auto_approve": True})
        status = {row["user_id"]: row["status"] for row in r.json()["links"]}
        assert status == {BOB: "approved", CAROL: "rejected"}
        assert any(n["title"] == "Telegram access approved" for n in await db.list_notifications(BOB))
        # Removing the token keeps the choice for the next one.
        c.delete("/fd/admin/telegram-shared", headers=_bearer(ALICE, "admin"))
        assert (await ts.load_config(db))["auto_approve"] is True
        # …and switching it off sends new requests to an admin again.
        await _bot_on(db)
        c.put("/fd/admin/telegram-shared", headers=_bearer(ALICE, "admin"), json={"auto_approve": False})
        c.delete("/fd/telegram-shared", headers=_bearer(BOB))
        r = c.post("/fd/telegram-shared/request", headers=_bearer(BOB), json={"agent_slug": "bob-bot"})
        assert r.json()["link"]["status"] == "pending"


class TestRelay:
    async def test_get_updates_and_send_within_own_chats(self, db, tg, agents):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await _link(db, CAROL, "carol-bot", TG_CAROL)
        await ts.route_update(db, _msg(5, TG_BOB, "hello agent"))
        c = _client()
        r = c.get("/fd/telegram-relay/botshared/getUpdates", params={"timeout": 0},
                  headers={"X-Agent-Auth": "tok-bob"})
        assert r.status_code == 200
        assert [u["message"]["text"] for u in r.json()["result"]] == ["hello agent"]
        assert c.get("/fd/telegram-relay/botshared/getUpdates", params={"timeout": 0},
                     headers={"X-Agent-Auth": "tok-carol"}).json()["result"] == []

        before = len(tg.calls)
        r = c.post("/fd/telegram-relay/botshared/sendMessage", headers={"X-Agent-Auth": "tok-bob"},
                   json={"chat_id": TG_BOB, "text": "<b>hi</b>", "parse_mode": "HTML"})
        assert r.status_code == 200 and r.json()["ok"] is True
        (call,) = tg.calls[before:]
        assert call["token"] == BOT_TOKEN and call["body"]["chat_id"] == TG_BOB

        r = c.post("/fd/telegram-relay/botshared/sendMessage", headers={"X-Agent-Auth": "tok-bob"},
                   json={"chat_id": TG_CAROL, "text": "sneaky"})
        assert r.status_code == 403 and r.json()["ok"] is False
        assert len(tg.calls) == before + 1

    async def test_uploads_are_forwarded_as_multipart(self, db, tg, agents):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        r = _client().post("/fd/telegram-relay/botshared/sendDocument",
                           headers={"X-Agent-Auth": "tok-bob"},
                           data={"chat_id": str(TG_BOB), "caption": "report"},
                           files={"document": ("r.pdf", b"%PDF-1", "application/pdf")})
        assert r.status_code == 200, r.text
        call = _sent(tg, "sendDocument")[-1]
        assert "multipart/form-data" in call["ctype"] and b"%PDF-1" in call["raw"]

    async def test_unknown_agents_and_browsers_are_refused(self, db, tg, agents):
        await _bot_on(db)
        c = _client()
        assert c.get("/fd/telegram-relay/botshared/getUpdates",
                     headers={"X-Agent-Auth": "nope"}).status_code == 403
        assert c.get("/fd/telegram-relay/botshared/getUpdates").status_code == 403
        assert c.get("/fd/telegram-relay/botshared/getUpdates",
                     headers={"X-Agent-Auth": "tok-bob", "Origin": "http://localhost"}).status_code == 403

    async def test_commands_land_on_the_agents_own_chats(self, db, tg, agents):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        r = _client().post("/fd/telegram-relay/botshared/setMyCommands", headers={"X-Agent-Auth": "tok-bob"},
                           json={"commands": [{"command": "new", "description": "New session"}]})
        assert r.json() == {"ok": True, "result": True}
        (call,) = _sent(tg, "setMyCommands")
        assert call["body"]["scope"] == {"type": "chat", "chat_id": TG_BOB}

    async def test_files_only_after_get_file(self, db, tg, agents):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _msg(1, TG_BOB, document={"file_id": "doc-1", "file_name": "a.pdf"}))
        c = _client()
        h = {"X-Agent-Auth": "tok-bob"}
        assert c.get("/fd/telegram-relay/file/botshared/photos/file_1.jpg", headers=h).status_code == 403
        r = c.post("/fd/telegram-relay/botshared/getFile", headers=h, json={"file_id": "doc-1"})
        assert r.json()["result"]["file_path"] == "photos/file_1.jpg"
        r = c.get("/fd/telegram-relay/file/botshared/photos/file_1.jpg", headers=h)
        assert r.status_code == 200 and r.content == b"PHOTO"
        assert c.get("/fd/telegram-relay/file/botshared/photos/file_1.jpg",
                     headers={"X-Agent-Auth": "tok-carol"}).status_code == 403
