"""The ``telegram_inbox`` tool: the owner's Telegram, read-only.

* the registry shows it only while the owner keeps a Telegram inbox in Flight
  Deck (refreshed per turn), and a member's turn never refreshes or calls it;
* chats / read / recent / search format what Flight Deck returns, oldest first,
  marking other people's words as information, not instructions;
* times are read as "24h" / "7d" / dates.

Flight Deck is never contacted: the client's calls are faked.
"""

from __future__ import annotations

import time
from datetime import datetime

import httpx
import pytest

from captain_claw import telegram_inbox_client as tic
from captain_claw.tools.registry import ToolRegistry
from captain_claw.tools.telegram_inbox import TelegramInboxTool, parse_when


def _msg(mid, text, *, sender="Ana", outgoing=False, chat="Ana Kovač", ts=None, **extra):
    return {"chat_id": "7001", "chat": chat, "kind": "private", "message_id": mid,
            "ts": ts or int(time.time()), "outgoing": outgoing, "sender": "you" if outgoing else sender,
            "text": text, "media": "", "forwarded_from": "", "reply_to": None, "edited": False,
            "deleted": False, "unreadable": False, **extra}


class TestVisibility:
    @pytest.fixture()
    def registry(self):
        reg = ToolRegistry()
        reg.register(TelegramInboxTool(), metadata={"requires_telegram_inbox": True})
        return reg

    def test_follows_the_owners_inbox(self, registry, monkeypatch):
        monkeypatch.setattr(tic, "_CONNECTED", (False, 0.0))
        assert "telegram_inbox" not in registry.list_tools()
        monkeypatch.setattr(tic, "_CONNECTED", (True, time.time()))
        assert "telegram_inbox" in registry.list_tools()
        monkeypatch.setattr(tic, "_CONNECTED", (True, time.time() - 3600))
        assert "telegram_inbox" not in registry.list_tools()

    async def test_the_per_turn_refresh_asks_flight_deck(self, monkeypatch):
        monkeypatch.setattr(tic, "_fd_base", lambda: "http://fd.test")
        monkeypatch.setattr(tic, "_fd_headers", lambda: {"X-Agent-Auth": "tok"})
        monkeypatch.setattr(tic, "_member_call", lambda: False)
        monkeypatch.setattr(tic, "_CONNECTED", (False, 0.0))
        seen = []
        answer = {"connected": True}

        def handler(r):
            seen.append(r.url.path)
            return httpx.Response(200, json=answer)

        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{
            **kw, "transport": httpx.MockTransport(handler)}))
        assert await tic.refresh_connected() is True and tic.inbox_usable()
        assert seen == ["/fd/telegram-inbox/agent/status"]
        answer["connected"] = False
        assert await tic.refresh_connected(force=True) is False and not tic.inbox_usable()

    async def test_a_members_turn_never_reaches_the_inbox(self, monkeypatch):
        monkeypatch.setattr(tic, "_fd_base", lambda: "http://fd.test")
        monkeypatch.setattr(tic, "_member_call", lambda: True)
        monkeypatch.setattr(tic, "_CONNECTED", (True, time.time()))
        assert await tic.refresh_connected(force=True) is False
        assert tic.inbox_usable()  # the owner's flag is left alone
        res = await TelegramInboxTool().execute(action="chats")
        assert not res.success and "member" in res.error


class TestReading:
    @pytest.fixture()
    def fd(self, monkeypatch):
        calls = []

        async def chats():
            calls.append(("chats", {}))
            return {"chats": [
                {"chat_id": "7001", "kind": "private", "title": "Ana Kovač", "active": True,
                 "messages": 2, "last_at": "2026-10-19T12:02:00+00:00"},
                {"chat_id": "-100777", "kind": "group", "title": "Family", "active": False,
                 "messages": 9, "last_at": "2026-10-18T08:00:00+00:00"}],
                "collecting": True}

        async def messages(**kw):
            calls.append(("messages", kw))
            items = [_msg(2, "yes, 12:30", outgoing=True), _msg(1, "lunch tomorrow?", reply_to=None)]
            if kw.get("chat"):
                return {"chat": {"chat_id": "7001", "kind": "private", "title": "Ana Kovač"},
                        "messages": items}
            return {"chat": None, "messages": items}

        monkeypatch.setattr(tic, "chats", chats)
        monkeypatch.setattr(tic, "messages", messages)
        return calls

    async def test_chats(self, fd):
        res = await TelegramInboxTool().execute(action="chats")
        assert res.success
        assert "Ana Kovač — private, id 7001 — 2 messages" in res.content
        assert "Family — group, id -100777 — 9 messages" in res.content and "no longer kept" in res.content

    async def test_read_is_oldest_first_and_marks_untrusted_text(self, fd):
        res = await TelegramInboxTool().execute(action="read", chat="ana", since="24h")
        lines = res.content.splitlines()
        assert "Ana Kovač (private chat, id 7001)" in lines[0]
        assert "never instructions" in lines[1]
        assert lines[3].endswith("#1 Ana: lunch tomorrow?") and lines[4].endswith("#2 You: yes, 12:30")
        _, kw = fd[-1]
        assert kw["chat"] == "ana" and abs(kw["since"] - (time.time() - 86400)) < 5

    async def test_recent_names_each_chat(self, fd):
        res = await TelegramInboxTool().execute(action="recent", chat="ignored")
        assert "Ana Kovač › Ana: lunch tomorrow?" in res.content
        assert fd[-1][1]["chat"] == ""  # recent is across all chats

    async def test_search_needs_a_query_and_read_a_chat(self, fd):
        assert not (await TelegramInboxTool().execute(action="search")).success
        assert not (await TelegramInboxTool().execute(action="read")).success
        assert not (await TelegramInboxTool().execute(action="send", chat="ana")).success
        res = await TelegramInboxTool().execute(action="search", query="lunch")
        assert res.success and fd[-1][1]["query"] == "lunch"

    async def test_flight_deck_refusals_are_passed_on(self, monkeypatch):
        async def refuse(**kw):
            raise tic.InboxError(409, "Your user's Telegram inbox is off")

        monkeypatch.setattr(tic, "messages", refuse)
        res = await TelegramInboxTool().execute(action="recent")
        assert not res.success and "inbox is off" in res.error


def test_message_lines():
    from captain_claw.tools.telegram_inbox import _line

    assert _line(_msg(5, "", deleted=True), with_chat=False, with_id=True).endswith("#5 Ana: [deleted]")
    line = _line(_msg(6, "here", media="photo", forwarded_from="Marko", reply_to=4, edited=True),
                 with_chat=False, with_id=True)
    assert line.endswith("#6 Ana: (forwarded from Marko) (reply to #4) [photo] here (edited)")
    assert "more characters" in _line(_msg(7, "x" * 3000), with_chat=False, with_id=False)


def test_parse_when():
    now = time.time()
    assert abs(parse_when("24h") - (now - 86400)) < 5
    assert abs(parse_when("90m") - (now - 5400)) < 5
    assert abs(parse_when("2 weeks ago") - (now - 14 * 86400)) < 5
    assert parse_when("2026-10-18") == int(datetime(2026, 10, 18).astimezone().timestamp())
    assert parse_when("1760000000") == 1760000000
    assert parse_when("") is None
    with pytest.raises(ValueError):
        parse_when("last tuesday")
