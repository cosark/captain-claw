"""An agent on Flight Deck's shared Telegram bot (web/telegram.py relay mode).

* An agent with a bot of its own keeps using it, exactly as before;
* under Flight Deck, an agent with none talks Telegram through FD's relay —
  the same bridge, its base URL pointed at FD, its identity in the headers;
* outside Flight Deck, or with ``telegram.shared_bot`` off, nothing starts;
* senders reaching it through the relay (FD admitted them) skip the agent's
  own pairing, and count as approved only there — never on an own bot.
"""

from __future__ import annotations

import json
import types

import httpx
import pytest

from captain_claw import fd_client
from captain_claw.config import TelegramConfig
from captain_claw.telegram_bridge import TelegramBridge
from captain_claw.web import telegram as tg

FD = "http://127.0.0.1:25080"
HEADERS = {"X-Agent-Auth": "agent-web-auth", "X-Agent-Secret": "deck-secret"}


def _server(*, enabled=False, token="", shared=True):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(telegram=TelegramConfig(
            enabled=enabled, bot_token=token, shared_bot=shared)),
        _telegram_enabled=bool(enabled and token),
        _telegram_bridge=None,
        _telegram_relay=False,
        _telegram_poll_task=None,
        _telegram_worker_task=None,
        _approved_telegram_users={},
        agent=None,
    )


@pytest.fixture
def fd(monkeypatch):
    async def idle(server):
        return None

    monkeypatch.setattr(tg, "_telegram_poll_loop", idle)
    monkeypatch.setattr(tg, "_telegram_worker", idle)
    monkeypatch.setattr(fd_client, "pinned_flight_deck_url", lambda: FD)
    monkeypatch.setattr(fd_client, "agent_identity_headers", lambda base: dict(HEADERS))


async def test_no_own_bot_under_flight_deck_uses_the_relay(fd):
    server = _server()
    await tg.start_telegram(server)
    bridge = server._telegram_bridge
    assert server._telegram_relay is True
    assert bridge.api_base_url == f"{FD}/fd/telegram-relay"
    assert bridge._client.headers["X-Agent-Auth"] == "agent-web-auth"
    await bridge.close()


async def test_an_own_bot_wins(fd):
    server = _server(enabled=True, token="111:own")
    await tg.start_telegram(server)
    assert server._telegram_relay is False
    assert server._telegram_bridge.api_base_url == "https://api.telegram.org"
    assert "X-Agent-Auth" not in server._telegram_bridge._client.headers
    await server._telegram_bridge.close()


async def test_nothing_starts_outside_flight_deck_or_when_opted_out(fd, monkeypatch):
    server = _server(shared=False)
    await tg.start_telegram(server)
    assert server._telegram_bridge is None
    monkeypatch.setattr(fd_client, "pinned_flight_deck_url", lambda: "")
    server = _server()
    await tg.start_telegram(server)
    assert server._telegram_bridge is None
    # Without its own identity FD couldn't tell which agent it is: no relay.
    monkeypatch.setattr(fd_client, "pinned_flight_deck_url", lambda: FD)
    monkeypatch.setattr(fd_client, "agent_identity_headers", lambda base: {})
    await tg.start_telegram(server)
    assert server._telegram_bridge is None


async def test_bridge_calls_go_to_the_relay_with_the_agents_identity():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    bridge = TelegramBridge("shared", f"{FD}/fd/telegram-relay", headers=HEADERS)
    bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=HEADERS)
    await bridge.send_message(77, "hello")
    (req,) = seen
    assert str(req.url) == f"{FD}/fd/telegram-relay/botshared/sendMessage"
    assert req.headers["X-Agent-Auth"] == "agent-web-auth"
    assert json.loads(req.content)["chat_id"] == 77
    await bridge.close()


async def test_relay_senders_are_admitted_only_on_the_relay(monkeypatch):
    saved = []

    async def _save(server):
        saved.append(dict(server._approved_telegram_users))

    monkeypatch.setattr(tg, "_tg_save_state", _save)
    server = _server()
    server._telegram_relay = True
    server._approved_telegram_users["9"] = {"user_id": 9, "chat_id": 9}  # paired on an own bot
    assert not tg._tg_is_approved(server, "5")
    await tg._tg_admit_relay_user(server, 5, 5, "bob", "Bob")
    assert tg._tg_is_approved(server, "5") and saved
    assert server._approved_telegram_users["5"]["via"] == "shared_bot"
    # The same agent later on a bot of its own: the relay admission doesn't carry over.
    server._telegram_relay = False
    assert not tg._tg_is_approved(server, "5")
    assert tg._tg_is_approved(server, "9")
