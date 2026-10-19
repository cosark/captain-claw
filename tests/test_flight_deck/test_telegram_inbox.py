"""The Telegram inbox (telegram_inbox.py + telegram_inbox_routes.py).

* a linked, consenting user's Chat Automation connection hands Flight Deck both
  sides of their private chats — kept sealed, edits followed, deletions honoured;
* groups are kept for the user who added the bot (or sent /inbox there); anyone
  else adding the bot gets it leaving at once;
* nothing of it ever reaches an agent's chat queue, and nothing is ever sent on
  the user's behalf;
* consent is explicit, off (or disconnecting Telegram) deletes everything;
* agents read only their own owner's inbox.

Telegram is never contacted: ``telegram_shared._http`` gets a mock transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.telegram_inbox as ti
import captain_claw.flight_deck.telegram_inbox_routes as tir
import captain_claw.flight_deck.telegram_shared as ts
import captain_claw.flight_deck.telegram_shared_routes as tsr
from captain_claw.flight_deck import agent_secret, auth, fd_crypto
from captain_claw.flight_deck.db import FlightDeckDB

BOB = "user-bob"
CAROL = "user-carol"
DAN = "user-dan"
BOT_TOKEN = "123456:SHARED-bot-token-xyz"
TG_BOB = 5001
TG_CAROL = 5002
TG_DAN = 5003
TG_ANA = 7001       # someone Bob chats with
TG_STRANGER = 5999
GROUP = -100777


@pytest.fixture()
async def db(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    for var in ("FD_LOCKDOWN", "FD_AGENT_SHARED_SECRET", "FD_DATA_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    agent_secret.reset_cache_for_tests()
    fd_crypto.reset_for_tests()
    d = FlightDeckDB(str(tmp_path / "fd.db"))
    await d.init()
    prev_db = auth._db
    auth.set_auth_db(d)
    now = "2026-01-01T00:00:00Z"
    for uid, email in [(BOB, "bob@x.co"), (CAROL, "carol@x.co"), (DAN, "dan@x.co")]:
        await d._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'user', ?, ?)",
            (uid, email, "h", uid.split("-")[1].title(), now, now),
        )
    await d._db.commit()
    monkeypatch.setattr(ts, "_state", ts._State())
    monkeypatch.setattr(ti, "_unknown", {})
    monkeypatch.setattr(ti, "_last_purge", 0.0)
    try:
        yield d
    finally:
        fd_crypto.reset_for_tests()
        agent_secret.reset_cache_for_tests()
        auth.set_auth_db(prev_db)
        await d.close()


@pytest.fixture()
def tg(monkeypatch):
    """A fake Telegram behind ``telegram_shared._http``; records every call."""
    state = types.SimpleNamespace(calls=[], connections={})

    def handler(request: httpx.Request) -> httpx.Response:
        _, bot, method = request.url.path.split("/", 2)
        body = json.loads(request.content) if request.content else {}
        state.calls.append({"method": method, "body": body})
        if bot[3:] != BOT_TOKEN:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 42, "is_bot": True, "first_name": "Team", "username": "team_bot",
                "can_connect_to_business": True, "can_read_all_group_messages": True}})
        if method == "getBusinessConnection":
            conn = state.connections.get(body.get("business_connection_id"))
            if conn is None:
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: not found"})
            return httpx.Response(200, json={"ok": True, "result": conn})
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(ts, "_http", lambda: httpx.AsyncClient(transport=transport))

    async def idle_poll(db, token):
        await asyncio.Event().wait()

    monkeypatch.setattr(ts, "_poll_loop", idle_poll)
    return state


@pytest.fixture()
def agents(monkeypatch):
    import captain_claw.flight_deck.server as srv

    registry = {"tok-bob": (BOB, "bob-bot"), "tok-bob2": (BOB, "bob-second"),
                "tok-carol": (CAROL, "carol-bot")}

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
    app.include_router(tsr.router)
    app.include_router(tir.router)
    return TestClient(app, client=("127.0.0.1", 50123))


def _bearer(uid, role="user"):
    return {"Authorization": f"Bearer {auth.create_access_token(uid, role)}"}


async def _bot_on(db, **extra):
    await ts.save_config(db, {"token": BOT_TOKEN, "enabled": True, "bot_username": "team_bot", **extra})
    await ts.start(db)


async def _link(db, user, tg_user, status="approved"):
    await db.create_telegram_link(user, f"{user}-agent", "Agent", ts.new_link_code())
    await db.update_telegram_link(user, status=status, tg_user_id=str(tg_user), link_code="")
    await ts.reload_links(db)


async def _inbox_on(db, user, tg_user):
    await _link(db, user, tg_user)
    await ti.set_enabled(db, user, enabled=True)


def _conn(cid, tg_user, enabled=True, **extra):
    return {"id": cid, "user": {"id": tg_user, "is_bot": False, "first_name": "Bob"},
            "user_chat_id": tg_user, "date": 1, "is_enabled": enabled, **extra}


_uid = iter(range(1000, 100000))


def _biz(cid, frm, chat, text, *, mid, key="business_message", date=None, **extra):
    who = {TG_BOB: "Bob", TG_ANA: "Ana", TG_CAROL: "Carol"}.get(frm, "X")
    return {"update_id": next(_uid), key: {
        "business_connection_id": cid, "message_id": mid, "date": date or int(time.time()),
        "from": {"id": frm, "is_bot": False, "first_name": who},
        "chat": {"id": chat, "type": "private", "first_name": "Ana", "last_name": "Kovač"},
        "text": text, **extra}}


def _group_msg(frm, text, *, mid, chat=GROUP, key="message", **extra):
    return {"update_id": next(_uid), key: {
        "message_id": mid, "date": int(time.time()), "text": text,
        "from": {"id": frm, "is_bot": False, "first_name": f"U{frm}"},
        "chat": {"id": chat, "type": "supergroup", "title": "Family"}, **extra}}


def _added(by, *, chat=GROUP, new="member", old="left"):
    return {"update_id": next(_uid), "my_chat_member": {
        "chat": {"id": chat, "type": "supergroup", "title": "Family"},
        "from": {"id": by, "is_bot": False, "first_name": f"U{by}"}, "date": 1,
        "old_chat_member": {"status": old, "user": {"id": 42, "is_bot": True}},
        "new_chat_member": {"status": new, "user": {"id": 42, "is_bot": True}}}}


def _sent(tg, method="sendMessage"):
    return [c for c in tg.calls if c["method"] == method]


async def _texts(db, user):
    return [(m["sender"], m["text"]) for m in reversed(await ti.messages(db, user))]


# ── private chats (Chat Automation) ─────────────────────────────────


class TestPrivateChats:
    async def test_both_sides_of_a_connected_chat_are_kept(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        assert await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)}) \
            == "inbox:connected"
        (hello,) = _sent(tg)
        assert hello["body"]["chat_id"] == TG_BOB and "never replies" in hello["body"]["text"]

        assert await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "lunch tomorrow?", mid=1)) \
            == "inbox:stored"
        assert await ts.route_update(db, _biz("c1", TG_BOB, TG_ANA, "yes, 12:30", mid=2)) \
            == "inbox:stored"
        assert await _texts(db, BOB) == [("Ana", "lunch tomorrow?"), ("you", "yes, 12:30")]
        (chat,) = await ti.chats(db, BOB)
        assert chat["title"] == "Ana Kovač" and chat["kind"] == "private" and chat["messages"] == 2
        # Never into an agent's chat queue, never a word to Ana.
        assert all(not b.queue for b in ts._state.boxes.values())
        assert all(c["body"].get("chat_id") != TG_ANA for c in tg.calls)

    async def test_text_and_names_are_sealed_at_rest(self, db, tg, tmp_path):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "my secret plan", mid=1))
        raw = (tmp_path / "fd.db").read_bytes() + b"".join(
            p.read_bytes() for p in tmp_path.glob("fd.db-wal"))
        assert b"secret plan" not in raw and "Kovač".encode() not in raw
        key = tmp_path / "fd-home" / "data_key"
        assert key.exists() and stat.S_IMODE(os.stat(key).st_mode) == 0o600

    async def test_edits_are_followed_and_deletions_drop_the_text(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "see you at 5", mid=1))
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "see you at 6", mid=1,
                                       key="edited_business_message", edit_date=2))
        (m,) = await ti.messages(db, BOB)
        assert m["text"] == "see you at 6" and m["edited"]
        assert await ts.route_update(db, {"update_id": 9, "deleted_business_messages": {
            "business_connection_id": "c1", "chat": {"id": TG_ANA, "type": "private"},
            "message_ids": [1]}}) == "inbox:deleted"
        (m,) = await ti.messages(db, BOB)
        assert m["deleted"] and m["text"] == "" and m["sender"] == "Ana"

    async def test_media_is_described(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        upd = _biz("c1", TG_ANA, TG_ANA, "", mid=1, caption="the venue",
                   photo=[{"file_id": "p1", "width": 1, "height": 1}])
        upd["business_message"].pop("text")
        await ts.route_update(db, upd)
        (m,) = await ti.messages(db, BOB)
        assert m["media"] == "photo" and m["text"] == "the venue"

    async def test_inbox_off_keeps_nothing_and_says_so(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, TG_BOB)
        assert await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)}) \
            == "inbox:connection-inbox-off"
        assert "inbox is off" in _sent(tg)[-1]["body"]["text"]
        assert await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "hi", mid=1)) == "inbox:off"
        assert await ti.messages(db, BOB) == []

    async def test_a_telegram_account_no_one_linked_is_refused(self, db, tg):
        await _bot_on(db)
        assert await ts.route_update(
            db, {"update_id": 1, "business_connection": _conn("cx", TG_STRANGER)}) \
            == "inbox:connection-refused"
        assert "only reads chats for Flight Deck users" in _sent(tg)[-1]["body"]["text"]
        assert await ts.route_update(db, _biz("cx", TG_ANA, TG_ANA, "hi", mid=1)) == "inbox:off"

    async def test_a_connection_made_while_fd_was_away_is_looked_up(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        tg.connections["c-late"] = _conn("c-late", TG_BOB)
        assert await ts.route_update(db, _biz("c-late", TG_ANA, TG_ANA, "hello?", mid=3)) \
            == "inbox:stored"
        assert [c["method"] for c in tg.calls].count("getBusinessConnection") == 1
        assert not _sent(tg)  # learned quietly

    async def test_admin_switch_off_stops_it(self, db, tg):
        await _bot_on(db, inbox=False)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        assert await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "hi", mid=1)) == "inbox:off"
        assert await ti.messages(db, BOB) == []


# ── groups ──────────────────────────────────────────────────────────


class TestGroups:
    async def test_the_adder_keeps_the_group_and_the_members_are_told(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        assert await ts.route_update(db, _added(TG_BOB)) == "inbox:group-joined"
        assert "won't post" in _sent(tg)[-1]["body"]["text"]
        assert await ts.route_update(db, _group_msg(TG_CAROL, "dinner at 8", mid=1)) == "inbox:group"
        assert await ts.route_update(db, _group_msg(TG_BOB, "👍", mid=2)) == "inbox:group"
        assert await _texts(db, BOB) == [("U5002", "dinner at 8"), ("you", "👍")]
        assert all(not b.queue for b in ts._state.boxes.values())

    async def test_anyone_else_adding_the_bot_makes_it_leave(self, db, tg):
        await _bot_on(db)
        await _link(db, CAROL, TG_CAROL)  # linked, but no inbox
        assert await ts.route_update(db, _added(TG_CAROL)) == "inbox:group-refused"
        assert _sent(tg, "leaveChat")[-1]["body"]["chat_id"] == GROUP
        assert await ts.route_update(db, _added(TG_STRANGER, chat=-1009)) == "inbox:group-refused"
        assert await ts.route_update(db, _group_msg(TG_CAROL, "x", mid=1)) == "ignored:group"

    async def test_inbox_command_adds_and_removes_a_member(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await _inbox_on(db, CAROL, TG_CAROL)
        await ts.route_update(db, _added(TG_BOB))
        assert await ts.route_update(db, _group_msg(TG_CAROL, "/inbox@team_bot", mid=1)) \
            == "inbox:group-on"
        assert _sent(tg)[-1]["body"]["chat_id"] == TG_CAROL  # told privately
        await ts.route_update(db, _group_msg(TG_DAN, "hello all", mid=2))
        assert await _texts(db, CAROL) == [("U5003", "hello all")]
        assert await _texts(db, BOB) == [("U5003", "hello all")]
        # Another bot's command isn't ours; a stranger's /inbox is ignored silently.
        assert await ts.route_update(db, _group_msg(TG_DAN, "/inbox@other_bot", mid=3)) == "inbox:group"
        n_sent = len(_sent(tg))
        assert await ts.route_update(db, _group_msg(TG_STRANGER, "/inbox", mid=4)) == "ignored:group"
        assert len(_sent(tg)) == n_sent
        # Both stop: the bot leaves.
        assert await ts.route_update(db, _group_msg(TG_CAROL, "/inbox off", mid=5)) == "inbox:group-off"
        assert not _sent(tg, "leaveChat")
        await ts.route_update(db, _group_msg(TG_BOB, "/inbox off", mid=6))
        assert _sent(tg, "leaveChat")[-1]["body"]["chat_id"] == GROUP

    async def test_removed_bot_stops_keeping_the_group(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        assert await ts.route_update(db, _added(TG_CAROL, new="kicked", old="member")) \
            == "inbox:group-left"
        assert await ts.route_update(db, _group_msg(TG_CAROL, "now private", mid=1)) == "ignored:group"

    async def test_group_upgraded_to_supergroup_is_still_kept(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, _added(TG_BOB, chat=-55))
        await ts.route_update(db, _group_msg(TG_CAROL, "before", mid=1, chat=-55))
        await ts.route_update(db, _group_msg(TG_CAROL, "", mid=2, chat=-55, migrate_to_chat_id=GROUP))
        # The supergroup numbers its messages from 1 again.
        assert await ts.route_update(db, _group_msg(TG_CAROL, "after", mid=1, chat=GROUP)) == "inbox:group"
        chats = {c["chat_id"]: c for c in await ti.chats(db, BOB)}
        assert chats[str(GROUP)]["active"] and chats[str(GROUP)]["messages"] == 1
        assert not chats["-55"]["active"] and chats["-55"]["messages"] == 1
        assert await _texts(db, BOB) == [("U5002", "before"), ("U5002", "after")]


# ── consent, deletion, retention ────────────────────────────────────


class TestConsentAndDeletion:
    async def test_turning_on_needs_a_linked_account_and_consent(self, db, tg):
        await _bot_on(db)
        c = _client()
        r = c.put("/fd/telegram-inbox", json={"enabled": True, "consent": True}, headers=_bearer(BOB))
        assert r.status_code == 409 and "Connect your Telegram" in r.json()["detail"]
        await _link(db, BOB, TG_BOB)
        r = c.put("/fd/telegram-inbox", json={"enabled": True}, headers=_bearer(BOB))
        assert r.status_code == 422
        r = c.put("/fd/telegram-inbox", json={"enabled": True, "consent": True, "retention_days": 90},
                  headers=_bearer(BOB))
        view = r.json()
        assert r.status_code == 200 and view["enabled"] and view["collecting"]
        assert view["retention_days"] == 90 and view["consented_at"]
        assert view["bot"] == {"business": True, "groups": True}
        r = c.put("/fd/telegram-inbox", json={"retention_days": 5}, headers=_bearer(BOB))
        assert r.status_code == 422

    async def test_off_deletes_everything_and_leaves_groups(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "hi", mid=1))
        await ts.route_update(db, _added(TG_BOB))
        r = _client().put("/fd/telegram-inbox", json={"enabled": False}, headers=_bearer(BOB))
        assert r.status_code == 200 and not r.json()["enabled"]
        assert await db.tg_inbox_stats(BOB) == {"messages": 0, "oldest": None, "newest": None, "chats": 0}
        assert await db.list_tg_inbox_chats(BOB) == []
        assert await db.list_tg_inbox_connections(BOB) == []
        assert _sent(tg, "leaveChat")[-1]["body"]["chat_id"] == GROUP

    async def test_disconnecting_telegram_deletes_the_inbox(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "hi", mid=1))
        assert _client().delete("/fd/telegram-shared", headers=_bearer(BOB)).status_code == 200
        assert (await db.tg_inbox_stats(BOB))["messages"] == 0
        assert not (await db.get_tg_inbox_settings(BOB))["enabled"]

    async def test_clear_keeps_the_inbox_on_and_the_groups(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        await ts.route_update(db, _group_msg(TG_CAROL, "x", mid=1))
        view = _client().delete("/fd/telegram-inbox/messages", headers=_bearer(BOB)).json()
        assert view["enabled"] and view["stats"]["messages"] == 0
        assert [g["chat_id"] for g in view["groups"]] == [str(GROUP)]
        assert await ts.route_update(db, _group_msg(TG_CAROL, "y", mid=2)) == "inbox:group"

    async def test_retention_drops_old_messages(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        old = int(time.time()) - 31 * 86400
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "ancient", mid=1, date=old))
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "fresh", mid=2))
        assert await ti.purge(db) == 1
        assert await _texts(db, BOB) == [("Ana", "fresh")]


# ── agents ──────────────────────────────────────────────────────────


class TestAgentRoutes:
    async def _seed(self, db, tg):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "the contract is signed", mid=1))
        await ts.route_update(db, _biz("c1", TG_BOB, TG_ANA, "great news", mid=2))

    async def test_owner_reads_lists_and_searches(self, db, tg, agents):
        await self._seed(db, tg)
        c = _client()
        h = {"X-Agent-Auth": "tok-bob"}
        assert c.get("/fd/telegram-inbox/agent/status", headers=h).json() == {"connected": True}
        chats = c.post("/fd/telegram-inbox/agent/chats", headers=h).json()["chats"]
        assert [ch["title"] for ch in chats] == ["Ana Kovač"]
        r = c.post("/fd/telegram-inbox/agent/messages", headers=h, json={"chat": "ana"}).json()
        assert r["chat"]["chat_id"] == str(TG_ANA) and len(r["messages"]) == 2
        r = c.post("/fd/telegram-inbox/agent/messages", headers=h, json={"query": "CONTRACT signed"}).json()
        assert [m["text"] for m in r["messages"]] == ["the contract is signed"]
        r = c.post("/fd/telegram-inbox/agent/messages", headers=h, json={"chat": "nobody"})
        assert r.status_code == 404

    async def test_another_users_agent_sees_nothing_of_it(self, db, tg, agents):
        await self._seed(db, tg)
        c = _client()
        h = {"X-Agent-Auth": "tok-carol"}
        assert c.get("/fd/telegram-inbox/agent/status", headers=h).json() == {"connected": False}
        assert c.post("/fd/telegram-inbox/agent/messages", headers=h, json={}).status_code == 409
        assert c.post("/fd/telegram-inbox/agent/messages", json={}).status_code == 403
        r = c.post("/fd/telegram-inbox/agent/messages", headers={**h, "Origin": "https://evil"}, json={})
        assert r.status_code == 403

    async def test_users_peek_shows_what_agents_see(self, db, tg, agents):
        await self._seed(db, tg)
        msgs = _client().get("/fd/telegram-inbox/recent", headers=_bearer(BOB)).json()["messages"]
        assert [m["text"] for m in msgs] == ["great news", "the contract is signed"]
        assert _client().get("/fd/telegram-inbox/recent", headers=_bearer(CAROL)).json() == {"messages": []}


# ── what's new, per agent ───────────────────────────────────────────


class TestNews:
    async def _setup(self, db):
        await _bot_on(db)
        await _inbox_on(db, BOB, TG_BOB)
        await ts.route_update(db, {"update_id": 1, "business_connection": _conn("c1", TG_BOB)})
        await ts.route_update(db, _added(TG_BOB))

    def _news(self, c, tok="tok-bob"):
        r = c.post("/fd/telegram-inbox/agent/news", headers={"X-Agent-Auth": tok})
        assert r.status_code == 200
        return r.json()

    @staticmethod
    def _counts(news):
        return {ch["title"]: ch["new"] for ch in news.get("chats", [])}

    async def test_counts_what_others_wrote_once(self, db, tg, agents):
        await self._setup(db)
        old = int(time.time()) - 2 * 86400
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "two days ago", mid=1, date=old))
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "a", mid=2))
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "b", mid=3))
        await ts.route_update(db, _biz("c1", TG_BOB, TG_ANA, "my own", mid=4))
        await ts.route_update(db, _group_msg(TG_CAROL, "family news", mid=1))
        c = _client()
        first = self._news(c)
        # The first look covers the last day; Bob's own message isn't news.
        assert first["first_look"] and first["total"] == 3
        assert self._counts(first) == {"Ana Kovač": 2, "Family": 1}
        assert {ch["kind"] for ch in first["chats"]} == {"private", "group"}
        assert self._news(c)["chats"] == []          # told once
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "c", mid=5))
        later = self._news(c)
        assert not later["first_look"] and self._counts(later) == {"Ana Kovač": 1}

    async def test_each_agent_has_its_own_look(self, db, tg, agents):
        await self._setup(db)
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "hi", mid=1))
        c = _client()
        assert self._counts(self._news(c, "tok-bob")) == {"Ana Kovač": 1}
        assert self._counts(self._news(c, "tok-bob2")) == {"Ana Kovač": 1}
        assert self._news(c, "tok-carol") == {"connected": False}

    async def test_reading_a_chat_counts_as_seen_but_searching_doesnt(self, db, tg, agents):
        await self._setup(db)
        c = _client()
        self._news(c)  # baseline
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "about the contract", mid=1))
        await ts.route_update(db, _group_msg(TG_CAROL, "contract too", mid=1))
        h = {"X-Agent-Auth": "tok-bob"}
        c.post("/fd/telegram-inbox/agent/messages", headers=h, json={"query": "contract"})
        c.post("/fd/telegram-inbox/agent/messages", headers=h, json={"chat": "ana"})
        assert self._counts(self._news(c)) == {"Family": 1}
        # Deleted messages aren't news either.
        await ts.route_update(db, _biz("c1", TG_ANA, TG_ANA, "oops", mid=2))
        await ts.route_update(db, {"update_id": 99, "deleted_business_messages": {
            "business_connection_id": "c1", "chat": {"id": TG_ANA, "type": "private"},
            "message_ids": [2]}})
        assert self._news(c)["chats"] == []

    async def test_off_means_no_news_and_no_cursor_left(self, db, tg, agents):
        await self._setup(db)
        c = _client()
        self._news(c)
        await ti.set_enabled(db, BOB, enabled=False)
        assert self._news(c) == {"connected": False}
        assert await db.tg_inbox_cursors(BOB, "bob-bot") == {}


# ── the agent's chat is untouched ───────────────────────────────────


async def test_private_chat_with_the_bot_still_reaches_the_agent(db, tg):
    await _bot_on(db)
    await _inbox_on(db, BOB, TG_BOB)
    upd = {"update_id": 77, "message": {
        "message_id": 1, "text": "hi agent", "from": {"id": TG_BOB, "is_bot": False, "first_name": "Bob"},
        "chat": {"id": TG_BOB, "type": "private"}}}
    assert await ts.route_update(db, upd) == "queued"
    assert await ti.messages(db, BOB) == []
