"""What's new on the owner's Telegram reaches the turn's context block.

On the owner's own live turn the agent asks Flight Deck once, at turn start,
and the request carries one line — counts and chat names, inside the internal
context block, ahead of the user's message. A scheduled turn neither asks nor
shows it (it would use up "new" before the user saw it).
"""

from __future__ import annotations

import pytest

from captain_claw import shared_usage, telegram_inbox_client
from captain_claw.agent import Agent
from captain_claw.llm import LLMProvider, LLMResponse
from captain_claw.session import Session
from captain_claw.tools.registry import ToolRegistry

_NEWS = {"connected": True, "first_look": False, "total": 3, "chats": [
    {"chat_id": "7001", "title": "Ana Kovač", "new": 2, "kind": "private"},
    {"chat_id": "-100777", "title": "Obitelj", "new": 1, "kind": "group"}]}


class _Stub(LLMProvider):
    provider = "openai"
    model = "stub"

    def __init__(self):
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        return LLMResponse(content="ok")

    async def complete_streaming(self, messages, tools=None, temperature=None, max_tokens=None):
        if False:
            yield ""

    def count_tokens(self, text):
        return len([p for p in (text or "").split() if p]) or 1


class _SessionManager:
    async def save_session(self, session):
        return None


@pytest.fixture
def fd(monkeypatch):
    asked = []

    async def news():
        asked.append(1)
        return _NEWS

    monkeypatch.setattr(telegram_inbox_client, "news", news)
    monkeypatch.setattr(shared_usage, "usage_allowed", lambda agent: True)
    return asked


def _agent(provider):
    agent = Agent(provider=provider)
    agent._initialized = True
    agent.session = Session(id="s1", name="default")
    agent.session_manager = _SessionManager()
    agent.tools = ToolRegistry()
    agent._build_env_now_text = lambda: ""
    return agent


@pytest.mark.asyncio
async def test_the_owners_turn_sees_whats_new(fd):
    provider = _Stub()
    await _agent(provider).complete("what's up?", turn_origin="human")
    last = provider.calls[0][-1]
    assert last.role == "user" and last.content.endswith("what's up?")
    block = last.content[:last.content.index("[END INTERNAL CONTEXT]")]
    assert ('Telegram since your last turn: 2 new from "Ana Kovač", 1 new in "Obitelj" (group).'
            in block)
    assert fd == [1]  # asked once per turn


@pytest.mark.asyncio
async def test_a_scheduled_turn_neither_asks_nor_shows(fd):
    provider = _Stub()
    await _agent(provider).complete("[Automated turn — cron. Not a live message from the user.] x",
                                    turn_origin="cron")
    assert fd == []
    assert all("Telegram since" not in str(m.content) for m in provider.calls[0])
