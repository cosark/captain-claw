"""In-turn context governor (Vatra workers): one long turn stays inside the window.

Measured on the glasses deck: a 131k-tier worker's estimate sat at ~117k while
the server received 160-191k — replayed reasoning was never counted for an
OpenAI-compatible thinking model, cl100k read number-heavy text low — and once
over budget the packer dropped one message per call, so the request prefix
moved (and the prompt cache missed) on every call. Governed:

* only the last few assistant steps carry reasoning back, and it is counted;
* the budget is divided by a ratio calibrated from provider-reported input;
* the chain is cut once, in one block, down to ~60%, on an assistant step, and
  the cut point is held so later calls only append;
* a context-overflow teaches the agent the server's window.
"""

from __future__ import annotations

import pytest

from captain_claw.agent import Agent
from captain_claw.config import get_config, set_config
from captain_claw.llm import LLMProvider, LLMResponse, Message, ToolDefinition
from captain_claw.session import Session


class _StubProvider(LLMProvider):
    provider = "openai"
    model = "stub-qwen"

    async def complete(self, messages: list[Message], tools: list[ToolDefinition] | None = None,
                       temperature: float | None = None, max_tokens: int | None = None) -> LLMResponse:
        return LLMResponse(content="ok")

    async def complete_streaming(self, messages, tools=None, temperature=None, max_tokens=None):
        if False:
            yield ""

    def count_tokens(self, text: str) -> int:
        return len([part for part in text.split() if part]) or 1


@pytest.fixture()
def budget():
    old = get_config().model_copy(deep=True)

    def _set(tokens: int) -> None:
        cfg = old.model_copy(deep=True)
        cfg.context.max_tokens = tokens
        set_config(cfg)

    yield _set
    set_config(old)


@pytest.fixture()
def governed(monkeypatch):
    monkeypatch.setenv("CLAW_CTX_GOVERNOR", "1")


def _agent() -> Agent:
    agent = Agent(provider=_StubProvider())
    agent.session = Session(id="s1", name="default")
    agent._build_env_now_text = lambda: ""
    agent._build_system_prompt = lambda: "system prompt"
    agent._last_tool_schema_tokens = 0
    return agent


def _call(cid: str) -> dict:
    return {"id": cid, "type": "function", "function": {"name": "dummy", "arguments": "{}"}}


def _turn(agent: Agent, steps: int, *, result_words: int = 50, reasoning_words: int = 20,
          start: int = 0) -> int:
    """A turn opener, then `steps` assistant tool-call steps (each with reasoning)
    and their results. Returns the turn start index."""
    if start == 0:
        turn_start = len(agent.session.messages)
        agent.session.add_message("user", "research the peers")
    else:
        turn_start = start
    base = len(agent.session.messages)
    for i in range(steps):
        cid = f"c{base}-{i}"
        agent.session.add_message("assistant", "", tool_calls=[_call(cid)])
        agent.session.messages[-1]["reasoning_content"] = " ".join(["think"] * reasoning_words)
        agent.session.add_message("tool", " ".join([f"r{i}"] * result_words),
                                  tool_call_id=cid, tool_name="dummy")
    return turn_start


def _sent(messages: list[Message]) -> list[Message]:
    return [m for m in messages if m.role != "system"]


# ── reasoning echo + counting ────────────────────────────────────────


def test_reasoning_is_counted_and_echoed_while_it_fits_its_share(governed, budget):
    budget(100_000)
    agent = _agent()
    turn_start = _turn(agent, 6)
    messages = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    full = [m for m in messages if m.role == "assistant" and m.reasoning_content
            and m.reasoning_content != "[earlier reasoning omitted]"]
    assert len(full) == 6
    window = agent.last_context_window
    assert window["governor"] == 1 and window["reasoning_in_budget"] == 1
    assert window["reasoning_tokens"] == 6 * 20
    assert window["prompt_tokens"] >= 2 + 6 * 50 + 6 * 20         # the echoed reasoning is counted


def test_reasoning_past_its_share_folds_to_constant_markers(governed, budget):
    budget(2_000)        # 25% share = ~500 tokens; 8 steps x 100 words of reasoning = 800
    agent = _agent()
    turn_start = _turn(agent, 8, result_words=5, reasoning_words=100)
    messages = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    steps = [m for m in messages if m.role == "assistant"]
    assert [m.reasoning_content == "[earlier reasoning omitted]" for m in steps] == [True] * 5 + [False] * 3
    assert all(m.reasoning_content for m in steps)                   # never an empty field
    # the next call (one more step) leaves those earlier messages untouched
    _turn(agent, 1, result_words=5, reasoning_words=100, start=turn_start)
    again = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert [m.reasoning_content for m in again if m.role == "assistant"][:8] == \
        [m.reasoning_content for m in steps]


def test_ungoverned_keeps_every_steps_reasoning(monkeypatch, budget):
    monkeypatch.delenv("CLAW_CTX_GOVERNOR", raising=False)
    budget(100_000)
    agent = _agent()
    turn_start = _turn(agent, 6)
    messages = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert sum(1 for m in messages if m.role == "assistant" and m.reasoning_content) == 6
    assert agent.last_context_window["governor"] == 0


# ── cut once and hold ────────────────────────────────────────────────


def _chain_ids(messages: list[Message]) -> list[str]:
    return [m.tool_call_id for m in messages if m.role == "tool"]


def test_over_budget_cuts_once_to_the_low_mark_on_an_assistant_step(governed, budget):
    budget(1_000)        # history budget ~ 1000 - system(2)
    agent = _agent()
    turn_start = _turn(agent, 20, result_words=60, reasoning_words=10)
    messages = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    window = agent.last_context_window
    hist = window["history_budget_tokens"]
    assert window["history_tokens"] <= int(agent._GOVERNOR_LOW * hist) + 80
    chain = _sent(messages)
    # the kept chain starts on an assistant step: the first sent tool result's
    # call is in the request
    sent_calls = {tc["id"] for m in messages if m.role == "assistant" for tc in (m.tool_calls or [])}
    assert all(tid in sent_calls for tid in _chain_ids(messages))
    assert chain[0].role == "user" and "research the peers" in chain[0].content
    assert agent._turn_window["floor"] is not None


def test_the_cut_is_held_so_later_calls_only_append(governed, budget):
    budget(1_000)
    agent = _agent()
    turn_start = _turn(agent, 20, result_words=60, reasoning_words=10)
    first = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    cuts = agent._turn_window_cuts
    kept_first = _chain_ids(first)
    _turn(agent, 1, result_words=60, reasoning_words=10, start=turn_start)
    second = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    kept_second = _chain_ids(second)
    assert agent._turn_window_cuts == cuts                 # no new cut
    assert kept_second[: len(kept_first)] == kept_first    # same prefix, one appended
    assert len(kept_second) == len(kept_first) + 1


def test_a_new_cut_comes_only_when_the_held_window_overflows(governed, budget):
    budget(1_000)
    agent = _agent()
    turn_start = _turn(agent, 20, result_words=60, reasoning_words=10)
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert agent._turn_window_cuts == 1
    for _ in range(12):
        _turn(agent, 1, result_words=60, reasoning_words=10, start=turn_start)
        agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert 1 < agent._turn_window_cuts <= 4                # a few block cuts, not one per call
    assert agent.last_context_window["history_tokens"] <= agent.last_context_window["history_budget_tokens"]


def test_a_forced_recut_cuts_even_when_it_fits(governed, budget):
    budget(100_000)
    agent = _agent()
    turn_start = _turn(agent, 10)
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert getattr(agent, "_turn_window_cuts", 0) == 0
    agent._turn_force_recut = True
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert agent._turn_window_cuts == 1
    assert agent._turn_force_recut is False


def test_the_turn_reset_forgets_the_cut(governed, budget):
    budget(1_000)
    agent = _agent()
    turn_start = _turn(agent, 20, result_words=60)
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert agent._turn_window is not None
    agent._reset_turn_window()
    assert agent._turn_window is None


# ── calibration and learned window ───────────────────────────────────


def test_calibration_is_an_ema_clamped_and_stepped():
    agent = _agent()
    assert agent._ctx_ratio() == 1.0
    agent._observe_ctx_calibration(100_000, 160_000)     # sample 1.6 → first sample taken whole
    assert agent._ctx_ratio() == 1.6
    agent._observe_ctx_calibration(100_000, 140_000)     # EMA → 1.5
    assert agent._ctx_ratio() == 1.5
    agent._observe_ctx_calibration(100_000, 10_000)      # implausible (0.1) → ignored
    assert agent._ctx_ratio() == 1.5
    agent._observe_ctx_calibration(100_000, 290_000)     # 2.9 → clamped to 2.0, EMA 1.75
    assert agent._ctx_ratio() == 1.75
    assert agent.session.metadata["ctx_calibration"]["ratio"] == pytest.approx(1.75)
    assert agent.session.metadata["ctx_calibration"]["model"] == "openai/stub-qwen"
    agent._observe_ctx_calibration(1_000, 3_000)         # tiny estimate → ignored
    assert agent._ctx_ratio() == 1.75


def test_ratio_shrinks_the_counted_history_budget(governed, budget):
    budget(100_000)
    agent = _agent()
    turn_start = _turn(agent, 2)
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    plain = agent.last_context_window["history_budget_tokens"]
    agent._set_calibration(1.5)
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert agent.last_context_window["history_budget_tokens"] == int(100_000 / 1.5) - 2
    assert agent.last_context_window["history_budget_tokens"] < plain
    assert agent.last_context_window["ctx_ratio"] == 1.5


def test_an_overflow_teaches_the_window_and_calibrates(governed, budget):
    budget(240_000)
    agent = _agent()
    agent.provider.max_tokens = 49_152
    agent.last_context_window = {"estimated_input_tokens": 100_000}
    agent._learn_ctx_window(131_072, prompt_tokens=150_000)
    assert agent._learned_window() == 131_072 - 131_072 // 4 - 1_024     # a modest output reserve
    assert agent._ctx_ratio() == 1.5
    turn_start = _turn(agent, 2)
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert agent.last_context_window["context_budget_tokens"] == agent._learned_window()


# ── review fixes ─────────────────────────────────────────────────────


def test_a_cut_keeps_the_newest_step_even_when_it_is_bigger_than_the_low_mark(governed, budget):
    budget(1_000)
    agent = _agent()
    turn_start = _turn(agent, 6, result_words=40, reasoning_words=5)
    # newest step: one call with four parallel results, ~600 tokens (> 60% low mark)
    agent.session.add_message("assistant", "", tool_calls=[_call(f"p{i}") for i in range(4)])
    for i in range(4):
        agent.session.add_message("tool", " ".join(["big"] * 150), tool_call_id=f"p{i}", tool_name="dummy")
    agent._turn_force_recut = True
    messages = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["p0", "p1", "p2", "p3"]


def test_earlier_turns_dont_force_a_cut_on_every_call(governed, budget):
    budget(2_000)
    agent = _agent()
    for i in range(12):                                   # earlier turns, ~600 tokens
        agent.session.add_message("user", f"earlier question {i} " + "w " * 20)
        agent.session.add_message("assistant", f"earlier answer {i} " + "w " * 25)
    turn_start = _turn(agent, 1, result_words=40)
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    for _ in range(25):
        _turn(agent, 1, result_words=40, start=turn_start)
        agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert getattr(agent, "_turn_window_cuts", 0) <= 6          # block cuts, not one per call


def test_calls_between_cuts_share_an_identical_prefix(governed, budget):
    budget(1_500)
    agent = _agent()
    turn_start = _turn(agent, 12, result_words=40, reasoning_words=30)
    agent._build_messages(tool_messages_from_index=turn_start, query="q")
    cuts = getattr(agent, "_turn_window_cuts", 0)
    prev = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    _turn(agent, 1, result_words=10, reasoning_words=5, start=turn_start)
    nxt = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert getattr(agent, "_turn_window_cuts", 0) == cuts
    key = lambda m: (m.role, m.content, m.tool_call_id, m.reasoning_content)  # noqa: E731
    assert [key(m) for m in nxt[: len(prev) - 1]] == [key(m) for m in prev[: len(prev) - 1]]


def test_a_tight_server_never_learns_a_starvation_window(budget):
    budget(32_768)
    agent = _agent()
    agent.provider.max_tokens = 65_536                    # the lithos case
    agent._learn_ctx_window(32_768, requested=65_536)
    assert agent._learned_window() >= 32_768 // 2


def test_calibration_and_window_belong_to_one_model(budget):
    budget(100_000)
    agent = _agent()
    agent._set_calibration(1.6)
    agent._learn_ctx_window(80_000)
    assert agent._ctx_ratio() == 1.6 and agent._learned_window() > 0
    agent.provider.model = "another-model"
    assert agent._ctx_ratio() == 1.0 and agent._learned_window() == 0


def test_overflow_wordings_without_numbers_are_recognised():
    from captain_claw.agent_orchestration_mixin import _looks_like_context_overflow

    for text in ("prompt is too long: 220000 tokens > 200000 maximum",
                 "the input token count (250000) exceeds the maximum number of tokens allowed",
                 "error: context_length_exceeded",
                 "request (240000 tokens) exceeds the available context size (n_ctx)"):
        assert _looks_like_context_overflow(text.lower())
    assert not _looks_like_context_overflow("rate limit exceeded, retry in 20s")


def test_bumping_calibration_tightens_the_budget():
    agent = _agent()
    agent._bump_ctx_calibration()
    assert agent._ctx_ratio() == 1.15
