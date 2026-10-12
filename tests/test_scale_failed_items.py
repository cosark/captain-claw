"""Scale micro-loop: a file it can't read is never silently counted as covered.

The Vatra incident: a Deep Researcher with ``pdf_extract`` but no
``xlsx_extract`` failed all six spreadsheets ("Tool not found: xlsx_extract",
one more "File not found" because the member name was cut to ``…ebitda- u``),
the loop marked them done, the summary said "16 of 16 processed" and the
coverage gate passed. Now the loop registers the missing extractor, resolves a
shortened name, records what still failed, says so plainly, and the gate holds
until a later tool call opens those files.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from captain_claw.agent_completion_mixin import AgentCompletionMixin
from captain_claw.agent_scale_detection_mixin import AgentScaleDetectionMixin
from captain_claw.agent_scale_loop_mixin import AgentScaleLoopMixin
from captain_claw.tools.document_extract import XlsxExtractTool

LONG_NAME = "2025 Group per company - consolidated revenues assets ebitda- ukupna lista.xlsx"
CUT_NAME = "2025 Group per company - consolidated revenues assets ebitda- u"


class _Tools:
    def __init__(self, base, names=()):
        self._names = set(names)
        self.registered: list = []
        self.runtime_base_path = base

    def has_tool(self, name):
        return name in self._names

    def register(self, tool, metadata=None):
        self._names.add(tool.name)
        self.registered.append(tool)


class _Result:
    def __init__(self, success=True, content="", error=""):
        self.success, self.content, self.error = success, content, error


class _Loop(AgentScaleLoopMixin, AgentScaleDetectionMixin, AgentCompletionMixin):
    """Duck-typed agent: real loop/summary/gate helpers, stubbed I/O + LLM."""

    def __init__(self, base, tool_names=("pdf_extract", "write"), unreadable=()):
        self.tools = _Tools(base, tool_names)
        self.workspace_base_path = base
        self.session = SimpleNamespace(messages=[], id="s1")
        self.calls: list[tuple[str, dict]] = []
        self.unreadable = set(unreadable)

    def _emit_tool_output(self, *a, **k):
        pass

    def _emit_thinking(self, *a, **k):
        pass

    def _count_tokens(self, text):
        return len(text or "") // 4

    def _chunked_processing_needed(self, *a, **k):
        return False

    async def _persist_scale_item_for_team(self, *a, **k):
        return None

    async def _complete_with_guards(self, *a, **k):
        return SimpleNamespace(content="a digest of the item", usage={})

    async def _execute_tool_with_guard(self, *, name, arguments, **kw):
        self.calls.append((name, dict(arguments)))
        if not self.tools.has_tool(name):
            raise RuntimeError(f"Tool not found: {name}")
        path = str(arguments.get("path", ""))
        if name == "write":
            return _Result(True, "written")
        if any(path.endswith(u) for u in self.unreadable):
            return _Result(False, error="Invalid XLSX: file is not a valid ZIP package")
        self.session.messages.append({"role": "tool", "tool_name": name,
                                      "tool_arguments": dict(arguments),
                                      "content": f"# {path}\nrows…"})
        return _Result(True, f"# {path}\nrows and rows of numbers")


@pytest.fixture()
def workspace(tmp_path):
    for n in ("a.pdf", "b.xlsx", LONG_NAME, "c.xlsx"):
        (tmp_path / n).write_bytes(b"x")
    return tmp_path


def _arm(loop, items):
    loop._scale_progress = {
        "items": list(items), "done_items": set(), "_output_strategy": "single_file",
        "_output_file": "out.md", "_final_action": "write_file",
    }


# ── the loop ─────────────────────────────────────────────────────────


async def test_missing_extractor_is_registered_and_cut_name_resolved(workspace):
    loop = _Loop(workspace)
    _arm(loop, ["a.pdf", "b.xlsx", CUT_NAME])
    res = await loop._run_scale_micro_loop(task_description="digest each file",
                                           output_file="out.md", turn_usage={})
    assert res["failed"] == 0, res["errors"]
    assert [t.name for t in loop.tools.registered] == ["xlsx_extract"]
    assert isinstance(loop.tools.registered[0], XlsxExtractTool)
    extracted = [args["path"] for name, args in loop.calls if name == "xlsx_extract"]
    assert extracted == ["b.xlsx", LONG_NAME]
    assert loop._scale_progress["failed_items"] == {}


async def test_failures_are_recorded_and_reported_not_counted(workspace):
    loop = _Loop(workspace, unreadable={"b.xlsx", "c.xlsx"})
    _arm(loop, ["a.pdf", "b.xlsx", "c.xlsx"])
    res = await loop._run_scale_micro_loop(task_description="digest each file",
                                           output_file="out.md", turn_usage={})
    sp = loop._scale_progress
    assert res["failed"] == 2
    assert set(sp["failed_items"]) == {"b.xlsx", "c.xlsx"}
    assert "Invalid XLSX" in sp["failed_items"]["b.xlsx"]
    summary = loop._build_micro_loop_summary(micro_result=res, scale_progress=sp,
                                             output_file="out.md")
    assert "1 of 3 items processed" in summary          # not "3 of 3"
    assert "NOT processed" in summary and "b.xlsx" in summary and "c.xlsx" in summary


async def test_a_later_success_clears_the_failure(workspace):
    loop = _Loop(workspace, unreadable={"b.xlsx"})
    _arm(loop, ["b.xlsx"])
    await loop._run_scale_micro_loop(task_description="t", output_file="out.md", turn_usage={})
    assert "b.xlsx" in loop._scale_progress["failed_items"]
    loop.unreadable.clear()
    loop._scale_progress["done_items"].discard("b.xlsx")
    await loop._run_scale_micro_loop(task_description="t", output_file="out.md", turn_usage={})
    assert loop._scale_progress["failed_items"] == {}


# ── helpers ──────────────────────────────────────────────────────────


def test_resolve_leaves_ambiguous_existing_and_urls_alone(tmp_path):
    (tmp_path / "report-2024.pdf").write_bytes(b"x")
    (tmp_path / "report-2025.pdf").write_bytes(b"x")
    loop = _Loop(tmp_path)
    assert loop._resolve_scale_file_item("report-20") == "report-20"          # two matches
    assert loop._resolve_scale_file_item("report-2024.pdf") == "report-2024.pdf"
    assert loop._resolve_scale_file_item("https://x.io/report") == "https://x.io/report"
    assert loop._resolve_scale_file_item("rep") == "rep"                      # too short
    assert loop._resolve_scale_file_item("report-2025") == "report-2025.pdf"


def test_ensure_extractor_only_registers_document_extractors(tmp_path):
    loop = _Loop(tmp_path, tool_names=("read",))
    assert loop._ensure_scale_extractor("read") is True
    assert loop._ensure_scale_extractor("web_fetch") is False
    assert loop._ensure_scale_extractor("docx_extract") is True
    assert [t.name for t in loop.tools.registered] == ["docx_extract"]


# ── the coverage gate's view ─────────────────────────────────────────


def _failed(loop, items, at):
    loop._scale_progress = {"failed_items": {i: "Tool not found" for i in items},
                            "failed_items_at": {i: at for i in items}}


def _tool(name, args, content="ok"):
    return {"role": "tool", "tool_name": name, "tool_arguments": args, "content": content}


def test_unread_until_a_later_call_opens_it(tmp_path):
    loop = _Loop(tmp_path)
    loop.session.messages = [
        _tool("glob", {"pattern": "*"}, "b.xlsx\nc.xlsx"),                 # before the failure
    ]
    _failed(loop, ["b.xlsx", "c.xlsx"], at=1)
    assert loop._unread_failed_scale_items(0) == ["b.xlsx", "c.xlsx"]
    loop.session.messages += [
        _tool("glob", {"pattern": "*.xlsx"}, "b.xlsx\nc.xlsx"),            # a listing isn't a read
        _tool("scale_micro_loop", {"item": "b.xlsx"}, "EXTRACT FAILED: b.xlsx"),
        _tool("shell", {"command": "python3 -c \"import openpyxl; openpyxl.load_workbook('b.xlsx')\""},
              "Error: no module"),                                          # failed call
    ]
    assert loop._unread_failed_scale_items(0) == ["b.xlsx", "c.xlsx"]
    loop.session.messages.append(_tool("shell", {"command": "python3 dump.py b.xlsx"}, "sheet1 …"))
    assert loop._unread_failed_scale_items(0) == ["c.xlsx"]
    loop.session.messages.append(_tool("shell", {"command": "python3 dump_all.py"}, "== c.xlsx ==\n…"))
    assert loop._unread_failed_scale_items(0) == []


def test_failures_from_an_earlier_turn_are_not_this_turns_business(tmp_path):
    loop = _Loop(tmp_path)
    loop.session.messages = [_tool("read", {"path": "x"})] * 5
    _failed(loop, ["b.xlsx"], at=2)
    assert loop._unread_failed_scale_items(4) == []       # failed before this turn began
    assert loop._unread_failed_scale_items(0) == ["b.xlsx"]
