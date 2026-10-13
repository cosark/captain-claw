"""In-turn context governor, phase 2: fold older tool results into file pointers.

A Vatra worker does its whole piece in one long turn. Past ~80% of the real
window the governor folds older tool results — in one batch, noted sources
first — into stable stubs (a short head + the file holding the full text),
down to ~50%. At ~70% a live note asks the worker to put what it still needs
into its notes file first; a notes write or a `vatra post` is the receipt.
"""

from __future__ import annotations

import pytest

from captain_claw.agent import Agent
from captain_claw.config import get_config, set_config
from captain_claw.context_spill import POINTER_MARK, notes_path, spill_view
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
    monkeypatch.delenv("CLAW_VFS_PROJECT", raising=False)
    monkeypatch.delenv("CLAW_VATRA_SUBTASK", raising=False)
    monkeypatch.delenv("CLAW_VATRA_SESSION", raising=False)


def _agent(tmp_path) -> Agent:
    agent = Agent(provider=_StubProvider())
    agent.session = Session(id="s1", name="default")
    agent.workspace_base_path = tmp_path
    agent._build_env_now_text = lambda: ""
    agent._build_system_prompt = lambda: "system prompt"
    agent._last_tool_schema_tokens = 0
    return agent


def _step(agent: Agent, tool: str, content: str, args: dict | None = None) -> int:
    """One assistant tool call and its result; returns the result's index."""
    cid = f"c{len(agent.session.messages)}"
    agent.session.add_message("assistant", "", tool_calls=[
        {"id": cid, "type": "function", "function": {"name": tool, "arguments": "{}"}}])
    agent.session.messages[-1]["reasoning_content"] = "think " * 5
    agent.session.add_message("tool", content, tool_call_id=cid, tool_name=tool,
                              tool_arguments=args or {})
    return len(agent.session.messages) - 1


def _page(i: int, words: int = 600) -> str:
    return " ".join([f"p{i}w"] * words)        # ~3-4 chars a word → well over 1500 chars


def _pressure(agent: Agent, real: int) -> None:
    """Pretend the last build measured `real` tokens, with nothing appended since."""
    agent.last_context_window = {"estimated_input_tokens": real}
    agent._last_build_msg_count = len(agent.session.messages)


def _research(agent: Agent, pages: int) -> tuple[int, list[int]]:
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "research the peers")
    idx = [_step(agent, "web_fetch", _page(i), {"url": f"https://ex.com/{i}"}) for i in range(pages)]
    return turn_start, idx


def test_below_the_nudge_mark_nothing_happens(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start, _ = _research(agent, 6)
    _pressure(agent, 6_000)
    assert agent._maybe_fold_turn_context(turn_start) == 0
    assert agent._context_pressure_note() == ""


def test_the_nudge_comes_once_before_any_fold(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start, idx = _research(agent, 6)
    _pressure(agent, 7_500)
    assert agent._maybe_fold_turn_context(turn_start) == 0
    note = agent._context_pressure_note()
    assert "notes.md" in note and "folded" in note
    assert not any(agent.session.messages[i].get("_folded") for i in idx)
    # once per cycle: the same pressure does not re-arm it
    agent._context_nudge_text = ""
    agent._maybe_fold_turn_context(turn_start)
    assert agent._context_pressure_note() == ""


def test_the_nudge_rides_in_the_request_as_a_live_note(governed, budget, tmp_path):
    budget(100_000)
    agent = _agent(tmp_path)
    turn_start, _ = _research(agent, 2)
    agent._context_nudge_text = "[Context is filling up] write your notes"
    messages = agent._build_messages(tool_messages_from_index=turn_start, query="q")
    assert any("[Context is filling up]" in (m.content or "") for m in messages)
    assert agent._last_build_msg_count == len(agent.session.messages)


def test_past_the_high_mark_older_results_fold_down_to_the_low_mark(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start, idx = _research(agent, 12)
    _pressure(agent, 8_500)
    folded = agent._maybe_fold_turn_context(turn_start)
    assert folded >= 1
    msgs = agent.session.messages
    # the newest two results are never folded
    assert not msgs[idx[-1]].get("_folded") and not msgs[idx[-2]].get("_folded")
    # oldest first, and only as many as needed: 8500 - 5000 = 3500 tokens ≈ 6 pages
    flags = [bool(msgs[i].get("_folded")) for i in idx]
    assert flags == sorted(flags, reverse=True)
    assert 5 <= folded <= 8
    # the stub keeps the key and a pointer to the full text, which is on disk
    stub = msgs[idx[0]]["content"]
    assert stub.startswith("[folded web_fetch https://ex.com/0 ")
    assert POINTER_MARK in stub
    pointer = stub.split(POINTER_MARK, 1)[1].split()[0]
    saved = tmp_path / pointer
    assert saved.is_file() and _page(0) in saved.read_text()
    assert agent._fold_count == 1


def test_a_second_pass_never_refolds_a_stub(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start, idx = _research(agent, 12)
    _pressure(agent, 8_500)
    agent._maybe_fold_turn_context(turn_start)
    stubs = {i: agent.session.messages[i]["content"] for i in idx
             if agent.session.messages[i].get("_folded")}
    _pressure(agent, 9_500)
    agent._maybe_fold_turn_context(turn_start)
    for i, text in stubs.items():
        assert agent.session.messages[i]["content"] == text


def test_sources_the_notes_cite_fold_first_and_receipts_stay(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "research the peers")
    unnoted = [_step(agent, "web_fetch", _page(i), {"url": f"https://ex.com/{i}"}) for i in range(3)]
    noted = _step(agent, "web_fetch", _page(7, 3_000), {"url": "https://ex.com/filing-2025"})
    receipt = _step(agent, "write", "Written 900 chars to notes.md " + "x " * 800,
                    {"path": "notes.md", "append": True,
                     "content": "## https://ex.com/filing-2025\n- revenue 4.2bn (2025)"})
    for i in range(2):
        _step(agent, "web_fetch", _page(20 + i), {"url": f"https://ex.com/l{i}"})
    _pressure(agent, 8_100)                 # need 3100: the big cited page + one more
    agent._maybe_fold_turn_context(turn_start)
    msgs = agent.session.messages
    assert msgs[noted].get("_folded")                            # the cited source went first
    assert msgs[unnoted[0]].get("_folded")
    assert not msgs[unnoted[1]].get("_folded") and not msgs[unnoted[2]].get("_folded")
    assert not msgs[receipt].get("_folded")                      # the receipt is never folded


def test_a_vatra_post_is_a_receipt_and_a_failed_write_is_not(governed, tmp_path):
    agent = _agent(tmp_path)
    post = {"role": "tool", "tool_name": "vatra", "content": "Shared to the team board",
            "tool_arguments": {"action": "post", "text": "x"}}
    failed = {"role": "tool", "tool_name": "write", "content": "Error: refused",
              "tool_arguments": {"path": "notes.md"}}
    deliverable = {"role": "tool", "tool_name": "write", "content": "Written",
                   "tool_arguments": {"path": "vfs:p/release-notes.md"}}
    assert agent._is_receipt(post)
    assert not agent._is_receipt(failed)
    assert not agent._is_receipt(deliverable)


def test_an_already_spilled_result_reuses_its_pointer(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    spilled = spill_view("web_fetch", "https://ex.com/a", _page(0), "vfs:p/sources/a-1234.md", 100_000)
    first = _step(agent, "web_fetch", spilled, {"url": "https://ex.com/a"})
    for i in range(4):
        _step(agent, "web_fetch", _page(i + 1), {"url": f"https://ex.com/{i}"})
    _pressure(agent, 9_000)
    agent._maybe_fold_turn_context(turn_start)
    stub = agent.session.messages[first]["content"]
    assert f"{POINTER_MARK}vfs:p/sources/a-1234.md" in stub
    assert not list((tmp_path / ".context").rglob("folded-web_fetch-a-*"))


def test_ungoverned_agents_never_fold(monkeypatch, budget, tmp_path):
    monkeypatch.delenv("CLAW_CTX_GOVERNOR", raising=False)
    budget(10_000)
    agent = _agent(tmp_path)
    assert agent._governor_on() is False


def test_a_short_notes_write_stays_inline(governed, tmp_path):
    agent = _agent(tmp_path)
    body = "## Source: https://ex.com/1\n- revenue 4.2bn (2025)\n" * 10
    cid = "w1"
    agent.session.add_message("assistant", "", tool_calls=[{
        "id": cid, "type": "function",
        "function": {"name": "write", "arguments": {"path": "notes.md", "content": body}}}])
    agent._compact_write_tool_call(cid, {"path": "notes.md", "content": body})
    args = agent.session.messages[-1]["tool_calls"][0]["function"]["arguments"]
    assert "revenue 4.2bn" in str(args)
    # a deliverable of the same size is still compacted
    agent.session.add_message("assistant", "", tool_calls=[{
        "id": "w2", "type": "function",
        "function": {"name": "write", "arguments": {"path": "report.md", "content": body}}}])
    agent._compact_write_tool_call("w2", {"path": "report.md", "content": body})
    args2 = agent.session.messages[-1]["tool_calls"][0]["function"]["arguments"]
    assert "revenue 4.2bn" not in str(args2)


def test_the_notes_path_lives_in_the_run_folder(monkeypatch):
    monkeypatch.delenv("CLAW_VATRA_SESSION", raising=False)
    monkeypatch.setenv("CLAW_VFS_PROJECT", "acme-run")
    monkeypatch.setenv("CLAW_VATRA_SUBTASK", "help:7")
    assert notes_path() == "vfs:acme-run/notes/help-7-notes.md"
    assert notes_path("p", "s2") == "vfs:p/notes/s2-notes.md"
    # a continuation round (same folder, same s1/s2 ids) gets its own file
    monkeypatch.setenv("CLAW_VATRA_SESSION", "0f1e2d3c-4b5a-6978")
    assert notes_path() == "vfs:acme-run/notes/0f1e2d3c-help-7-notes.md"
    monkeypatch.delenv("CLAW_VFS_PROJECT")
    assert notes_path() == "notes.md"


# ── review follow-ups ────────────────────────────────────────────────


def _parallel_step(agent: Agent, results: list[tuple[str, str, dict]]) -> list[int]:
    """One assistant step with several tool calls; returns the result indexes."""
    base = len(agent.session.messages)
    calls = [{"id": f"c{base}-{k}", "type": "function", "function": {"name": t, "arguments": "{}"}}
             for k, (t, _, _) in enumerate(results)]
    agent.session.add_message("assistant", "", tool_calls=calls)
    agent.session.messages[-1]["reasoning_content"] = "think " * 5
    idx = []
    for call, (tool, content, args) in zip(calls, results, strict=True):
        agent.session.add_message("tool", content, tool_call_id=call["id"], tool_name=tool,
                                  tool_arguments=args)
        idx.append(len(agent.session.messages) - 1)
    return idx


def _pointer(stub: str) -> str:
    return stub.split(POINTER_MARK, 1)[1].split()[0]


def test_each_folded_result_gets_its_own_file(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    a = _step(agent, "shell", _page(1), {"command": "ls -la"})
    b = _step(agent, "shell", _page(2), {"command": "ls -la"})
    c = _step(agent, "read", _page(3), {"path": "big.md", "offset": 1})
    d = _step(agent, "read", _page(4), {"path": "big.md", "offset": 400})
    for i in range(2):
        _step(agent, "web_fetch", _page(9 + i), {"url": f"https://ex.com/{i}"})
    _pressure(agent, 9_000)
    agent._maybe_fold_turn_context(turn_start)
    msgs = agent.session.messages
    pointers = [_pointer(msgs[i]["content"]) for i in (a, b, c, d)]
    assert len(set(pointers)) == 4
    for i, n in zip((a, b, c, d), (1, 2, 3, 4), strict=True):
        assert _page(n) in (tmp_path / _pointer(msgs[i]["content"])).read_text()
    assert msgs[a]["content"].startswith("[folded shell ls -la ")


def test_the_newest_steps_results_are_not_folded_before_the_model_sees_them(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start, older = _research(agent, 3)
    new = _parallel_step(agent, [("web_fetch", _page(20 + k), {"url": f"https://ex.com/n{k}"})
                                 for k in range(4)])
    _pressure(agent, 8_800)
    agent._maybe_fold_turn_context(turn_start)
    msgs = agent.session.messages
    assert all(msgs[i].get("_folded") for i in older)
    assert not any(msgs[i].get("_folded") for i in new)


def test_unseen_results_fold_only_when_the_request_would_not_fit(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    new = _parallel_step(agent, [("web_fetch", _page(30 + k), {"url": f"https://ex.com/u{k}"})
                                 for k in range(5)])
    _pressure(agent, 9_900)
    agent._maybe_fold_turn_context(turn_start)
    flags = [bool(agent.session.messages[i].get("_folded")) for i in new]
    assert flags[0] is True and flags[-2:] == [False, False]
    assert sum(flags) == 1                       # only as much as gets under the send limit


def test_notes_written_in_the_same_step_dont_count_as_noted(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    old = [_step(agent, "web_fetch", _page(i), {"url": f"https://ex.com/o{i}"}) for i in range(3)]
    same = _parallel_step(agent, [
        ("web_fetch", _page(40, 3_000), {"url": "https://ex.com/s0"}),
        ("write", "Written 300 chars to notes.md", {"path": "notes.md", "content": "https://ex.com/s0"}),
    ])
    _step(agent, "web_fetch", _page(50), {"url": "https://ex.com/z"})
    _step(agent, "web_fetch", _page(51), {"url": "https://ex.com/z2"})
    _pressure(agent, 8_100)                 # need 3100
    agent._maybe_fold_turn_context(turn_start)
    msgs = agent.session.messages
    # the notes were written before s0 was seen: s0 is not "noted", so the oldest
    # pages go first (were s0 noted, it alone + one page would have sufficed)
    assert all(msgs[i].get("_folded") for i in old)
    assert not msgs[same[1]].get("_folded")


def test_a_fold_that_frees_too_little_waits(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    small = _step(agent, "web_fetch", " ".join(["s"] * 520), {"url": "https://ex.com/small"})
    for i in range(2):
        _step(agent, "web_fetch", _page(i), {"url": f"https://ex.com/{i}"})
    _pressure(agent, 8_500)
    assert agent._maybe_fold_turn_context(turn_start) == 0
    assert not agent.session.messages[small].get("_folded")
    assert (getattr(agent, "_turn_window", None) or {}).get("echo_floor") is None


def test_nothing_folds_when_the_full_text_cant_be_saved(governed, budget, tmp_path, monkeypatch):
    from captain_claw import context_spill

    monkeypatch.setattr(context_spill, "save_source", lambda *a, **k: None)
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start, idx = _research(agent, 8)
    before = [agent.session.messages[i]["content"] for i in idx]
    _pressure(agent, 8_500)
    assert agent._maybe_fold_turn_context(turn_start) == 0
    assert [agent.session.messages[i]["content"] for i in idx] == before


def test_a_jump_past_the_high_mark_still_tells_the_model_once(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start, _ = _research(agent, 12)
    _pressure(agent, 8_500)                        # never saw the 70% note
    assert agent._maybe_fold_turn_context(turn_start) > 0
    assert agent._context_pressure_note().startswith("[Context folded]")
    _pressure(agent, 5_200)
    agent._maybe_fold_turn_context(turn_start)
    assert agent._context_pressure_note() == ""   # one call only


def test_a_seen_nudge_is_cleared_by_the_fold(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start, _ = _research(agent, 12)
    _pressure(agent, 7_500)
    agent._maybe_fold_turn_context(turn_start)
    assert agent._context_pressure_note().startswith("[Context is filling up]")
    _pressure(agent, 8_500)
    assert agent._maybe_fold_turn_context(turn_start) > 0
    assert agent._context_pressure_note() == ""


def test_replayed_reasoning_is_counted_once(governed, budget, tmp_path, monkeypatch):
    budget(100_000)
    agent = _agent(tmp_path)
    turn_start, _ = _research(agent, 1)
    agent._last_build_msg_count = len(agent.session.messages)
    agent.last_context_window = {"estimated_input_tokens": 1_000}
    agent.session.add_message("assistant", "", tool_calls=[_step_call := {
        "id": "cx", "type": "function", "function": {"name": "dummy", "arguments": "{}"}}])
    agent.session.messages[-1]["reasoning_content"] = " ".join(["deep"] * 400)
    monkeypatch.setattr(agent, "_replays_reasoning", lambda: True)
    replayed, _ = agent._turn_real_projection()
    monkeypatch.setattr(agent, "_replays_reasoning", lambda: False)
    echoed, _ = agent._turn_real_projection()
    assert _step_call["id"] == "cx"
    assert replayed == echoed                       # same 400 tokens either way, not 800
    assert 1_400 <= echoed < 1_500


def test_a_fold_lets_the_model_read_the_saved_copy(governed, budget, tmp_path):
    import os

    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    spilled = spill_view("web_fetch", "https://ex.com/a", _page(0), "vfs:p/sources/a-1.md", 100_000)
    first = _step(agent, "web_fetch", spilled, {"url": "https://ex.com/a"})
    for i in range(4):
        _step(agent, "web_fetch", _page(i + 1), {"url": f"https://ex.com/{i}"})
    sig = f"read|path={os.path.abspath('vfs:p/sources/a-1.md')}"
    agent._turn_tool_call_counts = {sig: 1, "read|path=/other": 1}
    _pressure(agent, 9_000)
    agent._maybe_fold_turn_context(turn_start)
    assert agent.session.messages[first].get("_folded")
    assert sig not in agent._turn_tool_call_counts
    assert "read|path=/other" in agent._turn_tool_call_counts


def test_the_nudge_names_vatra_only_inside_a_run(governed, monkeypatch, tmp_path):
    agent = _agent(tmp_path)
    assert "vatra" not in agent._nudge_text(folded=False)
    monkeypatch.setenv("CLAW_VATRA_SESSION", "0f1e2d3c-1111")
    monkeypatch.setenv("CLAW_VFS_PROJECT", "acme")
    monkeypatch.setenv("CLAW_VATRA_SUBTASK", "s2")
    text = agent._nudge_text(folded=False)
    assert "`vatra post`" in text and "vfs:acme/notes/0f1e2d3c-s2-notes.md" in text


# ── review pass 2 ────────────────────────────────────────────────────


def test_over_the_send_limit_read_results_fold_before_unread_ones(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    old = _step(agent, "web_fetch", _page(1, 520), {"url": "https://ex.com/old"})   # < 5% on its own
    new = _parallel_step(agent, [("web_fetch", _page(30 + k), {"url": f"https://ex.com/u{k}"})
                                 for k in range(3)])
    _pressure(agent, 9_900)
    agent._maybe_fold_turn_context(turn_start)
    msgs = agent.session.messages
    assert msgs[old].get("_folded")
    assert not any(msgs[i].get("_folded") for i in new)          # 9.9k - ~400 is under 97%
    assert (getattr(agent, "_turn_window", None) or {}).get("echo_floor") is not None


def test_folding_only_unread_results_keeps_the_echo_floor(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    _parallel_step(agent, [("web_fetch", _page(30 + k), {"url": f"https://ex.com/u{k}"})
                           for k in range(4)])
    _pressure(agent, 9_900)
    assert agent._maybe_fold_turn_context(turn_start) >= 1
    assert (getattr(agent, "_turn_window", None) or {}).get("echo_floor") is None


def test_fold_files_stay_apart_when_a_provider_reuses_call_ids(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    pages = []
    for n in (1, 2):
        agent.session.add_message("assistant", "", tool_calls=[
            {"id": "embedded_0", "type": "function", "function": {"name": "read", "arguments": "{}"}}])
        agent.session.add_message("tool", _page(n), tool_call_id="embedded_0", tool_name="read",
                                  tool_arguments={"path": "big.md"})
        pages.append(len(agent.session.messages) - 1)
    for i in range(6):
        _step(agent, "web_fetch", _page(10 + i), {"url": f"https://ex.com/{i}"})
    _pressure(agent, 9_000)
    agent._maybe_fold_turn_context(turn_start)
    p1, p2 = (_pointer(agent.session.messages[i]["content"]) for i in pages)
    assert p1 != p2
    assert _page(1) in (tmp_path / p1).read_text() and _page(2) in (tmp_path / p2).read_text()


def test_a_fold_also_frees_paged_reads_of_the_folded_file(governed, budget, tmp_path):
    import os

    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    page = _step(agent, "read", _page(1), {"path": "/data/src.md", "offset": 401})
    for i in range(4):
        _step(agent, "web_fetch", _page(10 + i), {"url": f"https://ex.com/{i}"})
    base = f"read|path={os.path.abspath('/data/src.md')}"
    agent._turn_tool_call_counts = {base: 1, base + "|offset=401": 1, "read|path=/data/src.md.bak": 1}
    _pressure(agent, 9_000)
    agent._maybe_fold_turn_context(turn_start)
    assert agent.session.messages[page].get("_folded")
    assert agent._turn_tool_call_counts == {"read|path=/data/src.md.bak": 1}


def test_a_citation_of_a_longer_url_does_not_note_its_prefix(governed, budget, tmp_path):
    budget(10_000)
    agent = _agent(tmp_path)
    turn_start = len(agent.session.messages)
    agent.session.add_message("user", "go")
    unnoted = [_step(agent, "web_fetch", _page(i), {"url": f"https://ex.com/o{i}"}) for i in range(3)]
    home = _step(agent, "web_fetch", _page(7, 3_000), {"url": "https://ex.com"})
    page = _step(agent, "web_fetch", _page(8, 3_000), {"url": "https://ex.com/pricing/"})
    _step(agent, "write", "Written to notes.md", {
        "path": "notes.md", "append": True,
        "content": "Source: https://ex.com/pricing. Plans from 9 EUR."})
    for i in range(2):
        _step(agent, "web_fetch", _page(20 + i), {"url": f"https://ex.com/l{i}"})
    _pressure(agent, 8_100)                 # need 3100: the cited page + one more
    agent._maybe_fold_turn_context(turn_start)
    msgs = agent.session.messages
    assert msgs[page].get("_folded")        # trailing slash and the sentence period match
    assert msgs[unnoted[0]].get("_folded")
    assert not msgs[home].get("_folded")    # "https://ex.com" is only a prefix of the citation


def test_only_the_governed_notes_shape_counts_as_notes_in_a_run(governed, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAW_VATRA_SESSION", "0f1e2d3c-1111")
    monkeypatch.setenv("CLAW_VFS_PROJECT", "acme")
    monkeypatch.setenv("CLAW_VATRA_SUBTASK", "s2")
    agent = _agent(tmp_path)
    assert agent._is_notes_path("vfs:acme/notes/0f1e2d3c-s2-notes.md")
    assert agent._is_notes_path("vfs:acme/notes/0f1e2d3c-s1-notes.md")   # a teammate's
    assert not agent._is_notes_path("vfs:acme/notes/models.py")
    assert not agent._is_notes_path("vfs:acme/notes.md")


# ── the duplicate guard treats the team board as live ────────────────


class _Board:
    name = "vatra"
    description = "board"
    parameters = {"type": "object", "properties": {}, "required": ["action"]}
    timeout_seconds = 5.0

    def __init__(self):
        self.ran: list[str] = []

    async def execute(self, action: str = "", **kwargs):
        from captain_claw.tools.registry import ToolResult

        self.ran.append(action)
        return ToolResult(success=True, content=f"{action} ok")


class _Work(_Board):
    name = "web_fetch"


def _loop_agent(tmp_path, *tools):
    from captain_claw.tools.registry import Tool, ToolRegistry

    class _NoSave:
        async def save_session(self, session) -> None:
            return None

    agent = Agent(provider=_StubProvider())
    agent._initialized = True
    agent.session = Session(id="s1", name="board")
    agent.session_manager = _NoSave()
    registry = ToolRegistry(base_path=tmp_path)
    for t in tools:
        registry.register(type(t.name, (Tool,), {
            "name": t.name, "description": t.description, "parameters": t.parameters,
            "execute": lambda self, _t=t, **kw: _t.execute(**kw)})())
    agent.tools = registry
    return agent


@pytest.fixture()
def dup_limit_one():
    old = get_config()
    cfg = old.model_copy(deep=True)
    cfg.tools.duplicate_call_max = 1
    set_config(cfg)
    yield
    set_config(old)


async def test_the_map_can_be_checked_again_after_other_work(dup_limit_one, tmp_path):
    from captain_claw.llm import ToolCall

    board, work = _Board(), _Work()
    agent = _loop_agent(tmp_path, board, work)
    await agent._handle_tool_calls([
        ToolCall(id="a", name="vatra", arguments={"action": "map"}),
        ToolCall(id="b", name="web_fetch", arguments={"action": "x", "url": "https://ex.com/a"}),
        ToolCall(id="c", name="vatra", arguments={"action": "map"}),
    ])
    assert board.ran == ["map", "map"]


async def test_back_to_back_map_polls_and_repeated_posts_are_still_blocked(dup_limit_one, tmp_path):
    from captain_claw.llm import ToolCall

    board, work = _Board(), _Work()
    agent = _loop_agent(tmp_path, board, work)
    results = await agent._handle_tool_calls([
        ToolCall(id="a", name="vatra", arguments={"action": "map"}),
        ToolCall(id="b", name="vatra", arguments={"action": "map"}),
        ToolCall(id="c", name="vatra", arguments={"action": "post", "text": "x"}),
        ToolCall(id="d", name="web_fetch", arguments={"action": "x", "url": "https://ex.com/a"}),
        ToolCall(id="e", name="vatra", arguments={"action": "post", "text": "x"}),
    ])
    assert board.ran == ["map", "post"]
    assert sum("DUPLICATE CALL BLOCKED" in str(r.get("content", "")) for r in results) == 2
