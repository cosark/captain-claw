"""Flight Deck chat log routes: paging, and the chat timeline's topics.

The chat is one continuous log per agent lane. A ``system`` row whose
metadata carries a ``topic`` object is a divider (the agent's context moved
to another session, or was reset); ``/topics`` lists them with the first
user message after each and how many turns followed, and ``since_id`` pages
the log from a divider on.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from captain_claw.flight_deck import auth as fd_auth
from captain_claw.flight_deck import rate_limiter, server
from captain_claw.flight_deck.auth import create_access_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

BOB = "user-bob"
CAROL = "user-carol"
REMOTE = ("203.0.113.9", 40001)


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Auth-enabled deck with two users."""
    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "fd.db")
    await db.init()
    set_auth_db(db)
    for uid, name in ((BOB, "Bob Builder"), (CAROL, "Carol")):
        await db._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
            " VALUES (?, ?, 'h', ?, 'user', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (uid, f"{uid}@x.co", name))
    await db._db.commit()
    monkeypatch.setattr(server, "AUTH_ENABLED", True)
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("FD_ALLOWED_HOSTS", "fd.test")
    monkeypatch.setattr(rate_limiter, "_api_limiter", rate_limiter._SlidingWindow())
    try:
        yield db
    finally:
        await db.close()
        fd_auth._db = prev


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, client=REMOTE), base_url="http://fd.test")


def _hdr(uid: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


def _turn(role: str, content: str) -> dict:
    return {"role": role, "content": content, "metadata": "{}"}


def _divider(agent_session_id: str, action: str) -> dict:
    return {
        "role": "system", "content": "New topic",
        "metadata": json.dumps({"topic": {
            "action": action, "agentSessionId": agent_session_id, "source": "web"}}),
    }


async def _seed(c: httpx.AsyncClient) -> list[int]:
    r = await c.post("/fd/chat/sessions", headers=_hdr(BOB),
                     json={"id": "proc-x", "agent_id": "proc-x", "agent_name": "X"})
    assert r.status_code == 200, r.text
    rows = [
        _turn("user", "hello"), _turn("assistant", "hi"),
        _divider("s1", "new"),
        _turn("user", "plan the trip"), _turn("assistant", "ok"),
        _divider("s2", "resume"),
        _turn("user", "/session switch s2"),   # a typed command: no title, no count
        _turn("user", "back again"),
    ]
    r = await c.post("/fd/chat/sessions/proc-x/messages", headers=_hdr(BOB), json={"messages": rows})
    assert r.status_code == 200, r.text
    ids = r.json()["ids"]
    assert len(ids) == len(rows)
    return ids


class TestTopics:
    async def test_lists_dividers_with_first_user_message_and_counts(self, deck):
        async with _client() as c:
            ids = await _seed(c)
            r = await c.get("/fd/chat/sessions/proc-x/topics", headers=_hdr(BOB))
        assert r.status_code == 200, r.text
        topics = r.json()
        assert [t["row_id"] for t in topics] == [ids[2], ids[5]]
        assert topics[0]["agent_session_id"] == "s1"
        assert topics[0]["action"] == "new"
        assert topics[0]["first_user_message"] == "plan the trip"
        assert topics[0]["message_count"] == 2
        assert topics[1]["agent_session_id"] == "s2"
        assert topics[1]["action"] == "resume"
        assert topics[1]["first_user_message"] == "back again"
        assert topics[1]["message_count"] == 1
        assert topics[0]["started_at"]

    async def test_a_log_without_dividers_has_no_topics(self, deck):
        async with _client() as c:
            r = await c.post("/fd/chat/sessions", headers=_hdr(BOB),
                             json={"id": "proc-y", "agent_id": "proc-y", "agent_name": "Y"})
            assert r.status_code == 200
            r = await c.post("/fd/chat/sessions/proc-y/messages", headers=_hdr(BOB),
                             json={"messages": [_turn("user", "hello"), _turn("assistant", "hi")]})
            assert r.status_code == 200
            r = await c.get("/fd/chat/sessions/proc-y/topics", headers=_hdr(BOB))
        assert r.status_code == 200
        assert r.json() == []

    async def test_other_users_see_nothing(self, deck):
        async with _client() as c:
            ids = await _seed(c)
            topics = await c.get("/fd/chat/sessions/proc-x/topics", headers=_hdr(CAROL))
            tail = await c.get(f"/fd/chat/sessions/proc-x/messages?since_id={ids[5]}", headers=_hdr(CAROL))
        assert topics.status_code == 200 and topics.json() == []
        assert tail.status_code == 200 and tail.json() == []


class TestTopicTitles:
    async def test_patch_names_a_topic_and_topics_show_it(self, deck):
        async with _client() as c:
            ids = await _seed(c)
            r = await c.patch(f"/fd/chat/sessions/proc-x/topics/{ids[2]}", headers=_hdr(BOB),
                              json={"title": "  Trip   planning  "})
            assert r.status_code == 200, r.text
            assert r.json() == {"ok": True, "title": "Trip planning"}
            topics = (await c.get("/fd/chat/sessions/proc-x/topics", headers=_hdr(BOB))).json()
        assert topics[0]["title"] == "Trip planning"
        assert topics[1]["title"] == ""

    async def test_patch_refuses_a_turn_and_another_users_log(self, deck):
        async with _client() as c:
            ids = await _seed(c)
            turn = await c.patch(f"/fd/chat/sessions/proc-x/topics/{ids[3]}", headers=_hdr(BOB),
                                 json={"title": "x"})
            foreign = await c.patch(f"/fd/chat/sessions/proc-x/topics/{ids[2]}", headers=_hdr(CAROL),
                                    json={"title": "x"})
            topics = (await c.get("/fd/chat/sessions/proc-x/topics", headers=_hdr(BOB))).json()
        assert turn.status_code == 404
        assert foreign.status_code == 404
        assert topics[0]["title"] == ""


class TestPaging:
    async def test_since_id_returns_the_log_from_that_row_on(self, deck):
        async with _client() as c:
            ids = await _seed(c)
            r = await c.get(f"/fd/chat/sessions/proc-x/messages?since_id={ids[5]}", headers=_hdr(BOB))
        assert r.status_code == 200, r.text
        rows = r.json()
        assert [row["id"] for row in rows] == [ids[5], ids[6], ids[7]]
        assert rows[0]["role"] == "system"
        assert json.loads(rows[0]["metadata"])["topic"]["agentSessionId"] == "s2"
        assert rows[2]["content"] == "back again"

    async def test_before_still_pages_backwards(self, deck):
        async with _client() as c:
            ids = await _seed(c)
            r = await c.get(f"/fd/chat/sessions/proc-x/messages?before={ids[2]}&limit=10", headers=_hdr(BOB))
        assert r.status_code == 200, r.text
        assert [row["content"] for row in r.json()] == ["hello", "hi"]
