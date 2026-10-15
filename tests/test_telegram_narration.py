"""Telegram narration goes out live, one message per blurb, like WhatsApp.

It used to be dropped from the live run (the Telegram agent had no narration
callback) and folded into one long final message joined with ``---``.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from captain_claw import garbled_reply
from captain_claw.web import telegram as tg

CHAT = 777


class _Bridge:
    """Records what reaches the chat; each send takes a moment, so blurbs
    sent back to back would overtake each other without the chain."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, *, reply_to_message_id=None):
        await asyncio.sleep(0.01 if text.startswith("first") else 0)
        self.sent.append(text)

    async def send_chat_action(self, chat_id, action):
        pass


class _Agent:
    def __init__(self, blurbs, answer="Done: here is the answer."):
        self.blurbs, self.answer = blurbs, answer
        self.session = None
        self.provider = None
        self.last_usage = self.total_usage = {}
        self.narration_callback = None
        self.seen_callback = None

    async def complete(self, text):
        self.seen_callback = self.narration_callback
        for i, b in enumerate(self.blurbs):
            if self.narration_callback:  # Agent._emit_narration
                self.narration_callback(b, i)
            await asyncio.sleep(0)
        return self.answer


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("STREAM_NARRATION", raising=False)
    bridge = _Bridge()
    broadcasts: list[dict] = []
    server = types.SimpleNamespace(
        _telegram_bridge=bridge, _telegram_user_locks={}, _orchestrator=None,
        _broadcast=broadcasts.append,
    )

    async def _not_garbled(reply, message=""):
        return False

    async def _no_steps(provider, response):
        return []

    monkeypatch.setattr(garbled_reply, "is_garbled", _not_garbled)
    monkeypatch.setattr(tg, "extract_next_steps", _no_steps)
    return types.SimpleNamespace(server=server, bridge=bridge, broadcasts=broadcasts)


async def _turn(env, agent):
    await tg._tg_process_with_typing(env.server, CHAT, "do it", 1, user_agent=agent, user_id="u1")


async def test_each_blurb_is_its_own_message_in_order_before_the_answer(env):
    agent = _Agent(["first, reading the file", "now applying the edits"])
    await _turn(env, agent)
    assert env.bridge.sent == [
        "first, reading the file", "now applying the edits", "Done: here is the answer.",
    ]
    assert agent.seen_callback is not None
    assert agent.narration_callback is None  # unwired after the turn


async def test_web_mirror_still_gets_the_whole_account(env):
    await _turn(env, _Agent(["first step"]))
    replies = [b["content"] for b in env.broadcasts if b.get("type") == "chat_message"]
    assert replies == ["first step\n\n---\n\nDone: here is the answer."]
    assert not any(b.get("type") == "narration" for b in env.broadcasts)  # never on the agent socket


async def test_stream_narration_off_folds_blurbs_into_the_answer(env, monkeypatch):
    monkeypatch.setenv("STREAM_NARRATION", "off")
    agent = _Agent(["first step"], answer="first step\n\n---\n\nDone.")
    await _turn(env, agent)
    assert agent.seen_callback is None  # the agent folds them itself
    assert env.bridge.sent == ["first step\n\n---\n\nDone."]


async def test_blurbs_land_before_the_error_when_the_turn_fails(env):
    class _Failing(_Agent):
        async def complete(self, text):
            self.narration_callback("first, looking around", 0)
            raise RuntimeError("boom")

    await _turn(env, _Failing([]))
    assert env.bridge.sent == ["first, looking around", "Error: boom"]
