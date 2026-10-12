"""Fetched pages come back whole on a large-context agent.

The configured max_chars (now 200000) is a floor for what the model asks:
models passed max_chars=4000-6000 "to save context" and lost the page. A larger
request is honoured. Whatever the number, one page stays within a share of the
agent's context budget so a small local window isn't handed a page it would drop
from its history whole. The task-contract prefetch no longer clips each source to
4500 characters, and the R10 corpus preview grew from 8000 to 50000.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from captain_claw.agent_reasoning_mixin import AgentReasoningMixin
from captain_claw.config import get_config, set_config
from captain_claw.tools import web_fetch as wf


@pytest.fixture()
def context_tokens():
    """Set context.max_tokens (and restore it)."""
    old = get_config().model_copy(deep=True)

    def _set(tokens: int, *, page: int | None = None, per_url: int | None = None,
             total: int | None = None) -> None:
        cfg = old.model_copy(deep=True)
        cfg.context.max_tokens = tokens
        if page is not None:
            cfg.tools.web_fetch.max_chars = page
        if per_url is not None:
            cfg.tools.web_fetch.batch_per_url_max_chars = per_url
        if total is not None:
            cfg.tools.web_fetch.batch_total_max_chars = total
        set_config(cfg)

    yield _set
    set_config(old)


def test_defaults_are_lifted():
    from captain_claw.config import WebFetchToolConfig

    cfg = WebFetchToolConfig()
    assert cfg.max_chars == 200_000
    assert cfg.batch_per_url_max_chars == 100_000
    assert cfg.batch_total_max_chars == 400_000
    assert wf._CORPUS_HEAD_CHARS == 50_000


def test_a_smaller_request_is_ignored_and_a_larger_one_honoured(context_tokens):
    context_tokens(262_144)                               # ceiling ≈ 367k chars
    assert wf._page_char_limit(6000, 200_000) == 200_000
    assert wf._page_char_limit(None, 200_000) == 200_000
    assert wf._page_char_limit(300_000, 200_000) == 300_000
    assert wf._page_char_limit("junk", 200_000) == 200_000


def test_one_page_stays_within_the_agents_context_share(context_tokens):
    context_tokens(32_000)                                # a small local window
    assert wf._page_char_limit(None, 200_000) == int(32_000 * 4 * wf._PAGE_CONTEXT_SHARE)
    assert wf._page_char_limit(10**9, 200_000) == int(32_000 * 4 * wf._PAGE_CONTEXT_SHARE)
    context_tokens(2_000)                                 # never below the floor ceiling
    assert wf._page_char_limit(None, 200_000) == wf._MIN_PAGE_CEILING


class _Resp:
    def __init__(self, text):
        self.text, self.status_code = text, 200

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self, text):
        self._text = text

    async def get(self, url, **kw):
        return _Resp(self._text)


async def test_web_fetch_returns_the_whole_page_despite_a_small_max_chars(context_tokens):
    context_tokens(262_144)
    body = "word " * 20_000                                    # 100k chars of text
    tool = wf.WebFetchTool()
    tool.client = _Client(f"<html><body><p>{body}</p></body></html>")
    res = await tool.execute(url="https://example.com/report", max_chars=6000, deep_fetch=False)
    assert res.success
    assert "[truncated]" not in res.content
    assert res.content.count("word") >= 19_900


async def test_web_fetch_respects_a_deliberately_small_config(context_tokens):
    context_tokens(262_144, page=20)
    tool = wf.WebFetchTool()
    tool.client = _Client("<html><body><p>abcdefghijklmnopqrstuvwxyz</p></body></html>")
    res = await tool.execute(url="https://example.com", deep_fetch=False)
    assert "... [truncated]" in res.content and "abcdefghijklmnopqrst" in res.content


def test_corpus_preview_is_bounded_by_the_page_limit():
    content = "§" * 120_000
    out = wf._corpus_output("https://e.com", 200, "text", 130_000, content, "vfs:p/sources/a.md")
    assert out.count("§") == wf._CORPUS_HEAD_CHARS
    small = wf._corpus_output("https://e.com", 200, "text", 130_000, content, "vfs:p/sources/a.md",
                              head_chars=30_000)
    assert small.count("§") == 30_000
    assert "preview only" in small


# ── task-contract prefetch: no 4500-char clip ────────────────────────


class _Prefetcher(AgentReasoningMixin):
    def __init__(self):
        self.calls: list[dict] = []
        self.tools = SimpleNamespace(list_tools=lambda: ["web_fetch"])

    async def _execute_tool_with_guard(self, *, name, arguments, **kw):
        self.calls.append(dict(arguments))
        return SimpleNamespace(success=True, content="page text", error="")

    def _add_session_message(self, **kw):
        pass

    def _emit_tool_output(self, *a, **k):
        pass


async def test_prefetch_uses_the_tools_own_page_size():
    agent = _Prefetcher()
    res = await agent._run_source_report_prefetch(["https://a.io", "https://b.io"], {})
    assert res["fetched"] == 2
    assert all("max_chars" not in c for c in agent.calls)


async def test_prefetch_still_honours_an_explicit_cap():
    agent = _Prefetcher()
    await agent._run_source_report_prefetch(["https://a.io"], {}, max_chars_per_source=90_000)
    assert agent.calls[0]["max_chars"] == 90_000


# ── review fixes ─────────────────────────────────────────────────────


def test_ceiling_follows_the_providers_clamped_window(context_tokens):
    """A local model clamped to 32k: the page is sized on 32k, not on the 160k config."""
    context_tokens(160_000)
    agent = SimpleNamespace(provider=SimpleNamespace(num_ctx=32_768))
    assert wf._context_char_ceiling(agent) == int(32_768 * 4 * wf._PAGE_CONTEXT_SHARE)
    assert wf._page_char_limit(None, 200_000, agent) == int(32_768 * 4 * wf._PAGE_CONTEXT_SHARE)
    hosted = SimpleNamespace(provider=SimpleNamespace(num_ctx=0))       # hosted: no num_ctx
    assert wf._context_char_ceiling(hosted) == int(160_000 * 4 * wf._PAGE_CONTEXT_SHARE)
    odd = SimpleNamespace(provider=SimpleNamespace(num_ctx="32768"))     # not an int → ignored
    assert wf._context_char_ceiling(odd) == int(160_000 * 4 * wf._PAGE_CONTEXT_SHARE)


async def test_web_fetch_sizes_pages_for_the_calling_agent(context_tokens):
    context_tokens(160_000)
    tool = wf.WebFetchTool()
    tool.client = _Client("<html><body><p>" + "word " * 40_000 + "</p></body></html>")
    agent = SimpleNamespace(provider=SimpleNamespace(num_ctx=32_768))
    res = await tool.execute(url="https://example.com/big", deep_fetch=False, _agent=agent)
    assert "[truncated]" in res.content
    assert len(res.content) < int(32_768 * 4 * wf._PAGE_CONTEXT_SHARE) + 500


class _BigPrefetcher(_Prefetcher):
    def __init__(self, page_chars):
        super().__init__()
        self.page_chars = page_chars
        self.messages: list[str] = []

    async def _execute_tool_with_guard(self, *, name, arguments, **kw):
        self.calls.append(dict(arguments))
        return SimpleNamespace(success=True, content="§" * self.page_chars, error="")

    def _add_session_message(self, **kw):
        self.messages.append(kw.get("content", ""))


async def test_prefetched_sources_share_one_pages_worth_of_context(context_tokens):
    context_tokens(160_000)
    agent = _BigPrefetcher(200_000)
    urls = [f"https://s{i}.io" for i in range(5)]
    await agent._run_source_report_prefetch(urls, {})
    ceiling = wf._context_char_ceiling(agent)
    kept = sum(m.count("§") for m in agent.messages)
    assert kept <= ceiling
    assert all(m.count("§") == ceiling // 5 for m in agent.messages)
    assert all("prefetched sources share the context" in m for m in agent.messages)


async def test_prefetch_never_goes_below_the_old_share(context_tokens):
    context_tokens(4_000)                               # tiny: ceiling floors at 20k chars
    agent = _BigPrefetcher(50_000)
    await agent._run_source_report_prefetch([f"https://s{i}.io" for i in range(20)], {})
    assert all(m.count("§") == 4500 for m in agent.messages)
