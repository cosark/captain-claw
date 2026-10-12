"""Governed tool results: big ones spill to files, pages are de-duplicated in a run.

A Vatra worker's single turn used to carry every page and extract in full until
the packer dropped them (one SEC 10-K fetch added ~50k real tokens). With the
governor on, a content tool's result over ~10% of the window is saved in full
and the model keeps its head + a pointer it can read/grep; web_fetch saves every
page to the run's shared sources/, so a teammate fetching the same URL gets the
saved copy instead of the page again.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from captain_claw import context_spill as cs
from captain_claw import write_guard
from captain_claw.config import get_config, set_config
from captain_claw.tools import web_fetch as wf
from captain_claw.tools.document_extract import _extract_char_limit


@pytest.fixture()
def ctx_tokens():
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


@pytest.fixture()
def run_folder(tmp_path, monkeypatch):
    """A Vatra worker bound to a run folder in a temp VFS."""
    monkeypatch.setenv("CLAW_VFS_ROOT", str(tmp_path / "vfs"))
    monkeypatch.setenv("CLAW_VFS_USER", "u1")
    monkeypatch.setenv("CLAW_VFS_PROJECT", "vatra-abc12345")
    monkeypatch.setenv("CLAW_VATRA_WORKER", "1")
    monkeypatch.setenv("CLAW_VATRA_OWNER", "market-scanner")
    return tmp_path / "vfs" / "u1" / "vatra-abc12345" / "sources"


def _agent(tmp_path, ratio: float = 1.0):
    return SimpleNamespace(
        workspace_base_path=str(tmp_path / "ws"), session=SimpleNamespace(id="sess1"),
        provider=SimpleNamespace(num_ctx=0), _ctx_ratio=lambda: ratio,
    )


# ── switches and sizes ───────────────────────────────────────────────


def test_governor_switch(monkeypatch):
    monkeypatch.delenv("CLAW_CTX_GOVERNOR", raising=False)
    assert cs.governor_enabled() is False
    monkeypatch.setenv("CLAW_CTX_GOVERNOR", "1")
    assert cs.governor_enabled() is True
    monkeypatch.setenv("CLAW_CTX_GOVERNOR", "off")
    assert cs.governor_enabled() is False


def test_inline_cap_is_a_tenth_of_the_real_window(tmp_path, ctx_tokens):
    ctx_tokens(100_000)
    assert cs.inline_cap_chars(_agent(tmp_path)) == 10_000 * 4
    assert cs.inline_cap_chars(_agent(tmp_path, ratio=1.6)) == int(10_000 * 4 / 1.6)
    ctx_tokens(1_000_000)
    assert cs.inline_cap_chars(_agent(tmp_path)) == 24_000 * 4        # capped
    small = _agent(tmp_path)
    small.provider.num_ctx = 20_000                                    # clamped local window
    ctx_tokens(160_000)
    assert cs.inline_cap_chars(small) == 4_000 * 4                    # floor


def test_url_files_are_named_like_the_r10_corpus():
    url = "https://www.sec.gov/Archives/edgar/data/1/x.htm"
    import hashlib

    expected = f"{wf._slugify_url(url)}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.md"
    assert cs.source_file_name("web_fetch", url) == expected


# ── spill ────────────────────────────────────────────────────────────


def test_off_means_untouched(tmp_path, monkeypatch, ctx_tokens):
    monkeypatch.delenv("CLAW_CTX_GOVERNOR", raising=False)
    ctx_tokens(10_000)
    big = "x" * 200_000
    assert cs.spill_tool_result(_agent(tmp_path), "pdf_extract", {"path": "a.pdf"}, big) is big


def test_a_big_extract_spills_privately_with_head_and_pointer(tmp_path, governed, ctx_tokens):
    ctx_tokens(100_000)
    agent = _agent(tmp_path)
    content = "".join(f"line {i}\n" for i in range(30_000))          # ~260k chars
    out = cs.spill_tool_result(agent, "pdf_extract", {"path": "reports/annual 2025.pdf"}, content)
    cap = cs.inline_cap_chars(agent)
    assert cs.POINTER_MARK in out and "preview only" in out
    assert len(out) < cap + 600
    pointer = out.split(cs.POINTER_MARK, 1)[1].split(" ", 1)[0]
    assert pointer.startswith(".context/sess1/pdf_extract-annual-2025.pdf-")
    saved = (tmp_path / "ws" / pointer).read_text()
    assert saved.endswith(content) and "# Source: reports/annual 2025.pdf" in saved


def test_small_results_and_already_saved_ones_pass_through(tmp_path, governed, ctx_tokens):
    ctx_tokens(100_000)
    agent = _agent(tmp_path)
    assert cs.spill_tool_result(agent, "pdf_extract", {"path": "a.pdf"}, "short") == "short"
    pointered = f"[web_fetch u · 9 chars · {cs.POINTER_MARK}vfs:p/sources/a.md]\n\n" + "y" * 900_000
    assert cs.spill_tool_result(agent, "web_fetch", {"url": "u"}, pointered) is pointered
    assert cs.spill_tool_result(agent, "read", {"path": "a"}, "z" * 900_000) == "z" * 900_000


def test_a_smaller_requested_max_chars_sets_the_preview(tmp_path, governed, ctx_tokens):
    ctx_tokens(100_000)
    out = cs.spill_tool_result(_agent(tmp_path), "xlsx_extract",
                               {"path": "model.xlsx", "max_chars": 1000}, "q" * 50_000)
    assert out.count("q") == 4_000                                       # floor of 4k


def test_extractors_take_the_whole_document_when_governed(monkeypatch):
    monkeypatch.delenv("CLAW_CTX_GOVERNOR", raising=False)
    assert _extract_char_limit(None) == 120_000
    monkeypatch.setenv("CLAW_CTX_GOVERNOR", "1")
    assert _extract_char_limit(None, tool_loop=True) == cs.SPILL_SOURCE_MAX
    assert _extract_char_limit(5000, tool_loop=True) == 5000


def test_pointers_are_refused_as_file_content():
    assert write_guard.is_placeholder_content(
        f"[web_fetch https://x.io/a · 120000 chars · {cs.POINTER_MARK}vfs:p/sources/a.md]\n\nbody")
    assert write_guard.is_placeholder_content(f"[{cs.DEDUPE_MARK} by analyst — {cs.POINTER_MARK}x]")
    assert not write_guard.is_placeholder_content("# Report\nWe fetched the page and found …")


# ── web_fetch: save every page, de-duplicate across the run ──────────


class _Resp:
    def __init__(self, text):
        self.text, self.status_code = text, 200

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self, text):
        self.text, self.calls = text, 0

    async def get(self, url, **kw):
        self.calls += 1
        return _Resp(self.text)


async def test_web_fetch_saves_the_page_and_a_teammate_gets_the_saved_copy(
        tmp_path, governed, run_folder, ctx_tokens):
    ctx_tokens(100_000)
    url = "https://investors.example.com/annual-report"
    html = "<html><body><p>" + "revenue " * 30_000 + "</p></body></html>"
    first = wf.WebFetchTool()
    first.client = _Client(html)
    res = await first.execute(url=url, deep_fetch=False, max_chars=6000, _agent=_agent(tmp_path))
    assert res.success and cs.POINTER_MARK in res.content
    assert res.content.count("revenue") < 1_000                          # the 6k preview, not the page
    saved = run_folder / cs.source_file_name("web_fetch", url)
    assert saved.is_file() and saved.read_text().count("revenue") == 30_000
    assert "by market-scanner" in saved.read_text()[:300]

    second = wf.WebFetchTool()
    second.client = _Client("<html>should not be fetched</html>")
    again = await second.execute(url=url, deep_fetch=False, _agent=_agent(tmp_path))
    assert second.client.calls == 0
    assert again.content.startswith(f"[{cs.DEDUPE_MARK} by market-scanner")
    assert "vfs:vatra-abc12345/sources/" in again.content
    assert again.content.count("revenue") < 1_000


async def test_a_substantial_page_is_shared_but_a_tiny_one_is_not(
        tmp_path, governed, run_folder, ctx_tokens):
    ctx_tokens(100_000)
    tool = wf.WebFetchTool()
    tool.client = _Client("<html><body><p>" + "useful text " * 200 + "</p></body></html>")
    res = await tool.execute(url="https://e.com/a", deep_fetch=False, _agent=_agent(tmp_path))
    assert cs.POINTER_MARK in res.content
    assert (run_folder / cs.source_file_name("web_fetch", "https://e.com/a")).is_file()
    tool.client = _Client("<html><body><p>tiny page</p></body></html>")
    res = await tool.execute(url="https://e.com/t", deep_fetch=False, _agent=_agent(tmp_path))
    assert "tiny page" in res.content and cs.POINTER_MARK not in res.content
    assert not (run_folder / cs.source_file_name("web_fetch", "https://e.com/t")).exists()


async def test_without_a_run_folder_small_pages_stay_inline(tmp_path, governed, monkeypatch, ctx_tokens):
    monkeypatch.delenv("CLAW_VFS_PROJECT", raising=False)
    ctx_tokens(100_000)
    tool = wf.WebFetchTool()
    tool.client = _Client("<html><body><p>tiny page</p></body></html>")
    res = await tool.execute(url="https://e.com/t", deep_fetch=False, _agent=_agent(tmp_path))
    assert "tiny page" in res.content and cs.POINTER_MARK not in res.content


async def test_batch_reuses_saved_pages_and_shares_one_inline_budget(
        tmp_path, governed, run_folder, ctx_tokens):
    ctx_tokens(100_000)
    agent = _agent(tmp_path)
    cs.save_source(agent, "web_fetch", "https://a.io/1", "alpha " * 20_000)
    tool = wf.WebFetchBatchTool()
    tool.client = _Client("<html><body><p>" + "beta " * 40_000 + "</p></body></html>")
    res = await tool.execute(urls=["https://a.io/1", "https://b.io/2", "https://c.io/3"],
                             deep_fetch=False, _agent=agent)
    assert res.success
    assert tool.client.calls == 2                                         # a.io was reused
    assert "1 already saved in this run" in res.content
    assert res.content.count(cs.POINTER_MARK) == 3
    assert len(res.content) < cs.inline_cap_chars(agent) + 4_000
    for u in ("https://b.io/2", "https://c.io/3"):
        assert (run_folder / cs.source_file_name("web_fetch", u)).is_file()


def test_old_saved_pages_are_fetched_again(tmp_path, governed, run_folder, monkeypatch):
    agent = _agent(tmp_path)
    cs.save_source(agent, "web_fetch", "https://a.io/1", "alpha " * 1000)
    assert cs.find_fetched("https://a.io/1") is not None
    monkeypatch.setattr(cs.time, "time", lambda: 10**12)
    assert cs.find_fetched("https://a.io/1") is None


# ── review fixes ─────────────────────────────────────────────────────


def test_only_web_fetch_writes_the_shared_page_namespace(tmp_path, governed, run_folder, ctx_tokens):
    ctx_tokens(100_000)
    url = "https://a.io/page"
    assert cs.source_file_name("web_get", url).startswith("web_get-")
    assert cs.source_file_name("web_get", url) != cs.source_file_name("web_fetch", url)
    out = cs.spill_tool_result(_agent(tmp_path), "web_get", {"url": url}, "<div>" * 50_000)
    assert ".context/sess1/web_get-" in out                     # private, never the run copy
    assert not (run_folder / cs.source_file_name("web_fetch", url)).exists()


def test_failed_and_challenge_pages_are_not_shared_or_reused(tmp_path, governed, run_folder):
    agent = _agent(tmp_path)
    assert not cs.shareable_page("x" * 5000, status=403)
    assert not cs.shareable_page("Just a moment... enable JavaScript and cookies to continue " * 20)
    assert cs.shareable_page("annual report text " * 200, status=200)
    cs.save_source(agent, "web_fetch", "https://a.io/forbidden", "denied " * 500, status=403)
    assert cs.find_fetched("https://a.io/forbidden") is None
    cs.save_source(agent, "web_fetch", "https://a.io/ok", "content " * 500, status=200)
    assert cs.find_fetched("https://a.io/ok") is not None


async def test_a_batch_salvage_is_never_the_runs_copy(tmp_path, governed, run_folder, ctx_tokens,
                                                    monkeypatch):
    ctx_tokens(100_000)
    tool = wf.WebFetchBatchTool()
    tool.client = _Client("<html><body><p>" + "shell " * 400 + "</p></body></html>")
    monkeypatch.setattr(wf, "_looks_thin", lambda *a, **k: True)     # needs a browser
    monkeypatch.setattr(wf, "_HAS_PLAYWRIGHT", False)                 # …which isn't there
    res = await tool.execute(urls=["https://js.io/app"], _agent=_agent(tmp_path))
    assert res.success
    assert not (run_folder / cs.source_file_name("web_fetch", "https://js.io/app")).exists()


def test_concurrent_saves_use_their_own_temp_files(tmp_path, governed, run_folder):
    agent = _agent(tmp_path)
    for _ in range(3):
        assert cs.save_source(agent, "web_fetch", "https://a.io/x", "content " * 400)
    assert not list(run_folder.glob("*.tmp"))


def test_a_failed_spill_still_bounds_the_inline_text(tmp_path, governed, ctx_tokens, monkeypatch):
    ctx_tokens(100_000)
    monkeypatch.setattr(cs, "save_source", lambda *a, **k: None)
    out = cs.spill_tool_result(_agent(tmp_path), "pdf_extract", {"path": "a.pdf"}, "z" * 500_000)
    assert len(out) < cs.inline_cap_chars(_agent(tmp_path)) + 300 and "could not be saved" in out


def test_marker_check_is_on_the_first_line_only(tmp_path, governed, ctx_tokens):
    ctx_tokens(100_000)
    page_about_it = "intro\n" + f"the tool said: {cs.POINTER_MARK}x" + ("w" * 500_000)
    out = cs.spill_tool_result(_agent(tmp_path), "pdf_extract", {"path": "doc.pdf"}, page_about_it)
    assert out != page_about_it and "preview only" in out


def test_extractors_lift_their_default_only_through_the_tool_loop(monkeypatch):
    monkeypatch.setenv("CLAW_CTX_GOVERNOR", "1")
    assert _extract_char_limit(None) == 120_000                       # google_drive / onedrive
    assert _extract_char_limit(None, tool_loop=True) == cs.SPILL_SOURCE_MAX


def test_pointer_lines_with_spaces_or_long_urls_are_refused():
    long_url = "https://x.io/" + "a" * 300
    assert write_guard.is_placeholder_content(
        f"[pdf_extract reports/annual report 2025.pdf · 9000 chars · {cs.POINTER_MARK}.context/s/x.md]")
    assert write_guard.is_placeholder_content(
        f"[web_fetch {long_url} · 9000 chars · {cs.POINTER_MARK}vfs:p/sources/x.md]")


def test_vatra_capture_skips_private_spills(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server as srv
    from captain_claw.flight_deck import vatra_routes as vr

    ws = tmp_path / "w1" / "data" / "workspace"
    (ws / ".context" / "s1").mkdir(parents=True)
    (ws / ".context" / "s1" / "pdf_extract-a.md").write_text("raw extract")
    (ws / "report.md").write_text("the deliverable")
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    files, text = vr._capture_generated("w1", set(), dest, "analyst", set())
    assert [f["name"] for f in files] == ["report.md"]
    assert "raw extract" not in text
