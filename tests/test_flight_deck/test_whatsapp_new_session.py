"""/new from WhatsApp: the person hears back, and the new session gets the
WhatsApp rules again.

The agent answers a slash command with a ``command_result`` on the commanding
socket; the channel-bus pump used to drop it, so a WhatsApp /new created the
session in silence. And the hidden rules block went out once per binding, so
the fresh session never saw it. Now the pump forwards command results (as a
plain-text notice) and resets ``context_sent`` when the agent's session
changes. The garbled-reply /new hint is a notice too: text only, no audio.
"""

from __future__ import annotations

import types

import pytest

from captain_claw.flight_deck import glasses_bridge as gb
from captain_claw.flight_deck import whatsapp_bridge as wb

WAID = "385911111111"
CHANNEL = f"whatsapp:{WAID}"
HINT = ("⚠️ That reply came out garbled — this session may be overloaded. "
        "Send /new to start a fresh session (the old one is kept in history).")


class _Resp:
    status_code = 200

    def json(self):
        return {"messages": [{"id": "wamid.out"}]}


class _Client:
    def __init__(self, sent):
        self.sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        if json.get("type") == "text":
            self.sent.append(json["text"]["body"])
        return _Resp()


@pytest.fixture
def wa(monkeypatch):
    """The real pump + WhatsApp relay, down to a fake Graph API."""
    sent: list = []
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WHATSAPP_ALLOWED_WAIDS", WAID)
    monkeypatch.delenv("WHATSAPP_AUDIO_REPLY", raising=False)
    monkeypatch.setattr(wb.httpx, "AsyncClient", lambda *a, **kw: _Client(sent))
    monkeypatch.setattr(wb, "_RECENT_REPLY_SENDS", {})
    monkeypatch.setattr(wb, "_MUTED_UNTIL", {})
    monkeypatch.setattr(wb, "_WIRED_CHANNELS", set())
    monkeypatch.setattr(wb, "_CHANNEL_WAIDS", {CHANNEL: {WAID}})
    ch = gb._ChannelState(channel_id=CHANNEL)
    monkeypatch.setitem(gb._channels, CHANNEL, ch)
    wb._ensure_whatsapp_forwarding(CHANNEL)

    async def _bc(_ch, payload):
        for cb in list(ch.callback_subscribers):
            await cb(payload)

    monkeypatch.setattr(gb, "_broadcast", _bc)

    async def frame(data):
        await gb._forward_agent_msg(ch, data)

    return types.SimpleNamespace(sent=sent, frame=frame, ch=ch)


async def test_the_new_command_result_reaches_whatsapp(wa):
    await wa.frame({"type": "command_result", "command": "/new",
                    "content": "New session created: **web-session** (`3f2a9c1b`)"})
    assert wa.sent == ["New session created: web-session (3f2a9c1b)"]


async def test_an_empty_command_result_sends_nothing(wa):
    await wa.frame({"type": "command_result", "command": "/x", "content": "  "})
    assert wa.sent == []


async def test_notices_go_as_plain_text_without_the_audio_extra(wa, monkeypatch):
    monkeypatch.setenv("WHATSAPP_AUDIO_REPLY", "on")
    audio: list = []

    async def _audio(waid, text):
        audio.append(text)

    monkeypatch.setattr(wb, "_send_whatsapp_audio", _audio)
    await wa.frame({"type": "chat_message", "role": "assistant", "content": HINT, "notice": True})
    await wa.frame({"type": "command_result", "command": "/stop", "content": "Stop signal sent."})
    assert wa.sent == [HINT, "Stop signal sent."]              # no "🎙 Generating audio…"
    assert audio == []


async def test_a_new_session_gets_the_rules_block_again(wa):
    await wa.frame({"type": "welcome", "session": {"id": "s1"}})
    wa.ch.context_sent = True                                  # the first message carried it
    await wa.frame({"type": "session_info", "id": "s1"})       # after every turn: no change
    assert wa.ch.context_sent is True
    await wa.frame({"type": "session_info", "id": "s2"})       # /new (from any surface)
    assert wa.ch.context_sent is False and wa.ch.agent_session_id == "s2"
    assert wa.sent == []                                       # nothing for the person


async def test_the_first_session_seen_never_resets(wa):
    wa.ch.context_sent = True
    await wa.frame({"type": "session_info", "id": "s9"})
    assert wa.ch.context_sent is True
    await wa.frame({"type": "session_info"})                   # no id: ignored
    assert wa.ch.agent_session_id == "s9"
