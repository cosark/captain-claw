"""Vatra workers: no dispatch time cap, the full reading kit, and an action log
that shows what a shell command actually ran.

- A Vatra run (``_run_extend_unbounded``) keeps extending a worker's turn for as
  long as the agent is connected; every other caller keeps the bounded ceiling.
- Every spawned Vatra agent gets the extraction + search tools on top of its
  archetype's list.
- ``_summarize_tool_args`` drops the leading ``cd <abs path> &&`` that alone
  filled the old 40-char cut (the log read like the agent only ran ``cd``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from captain_claw.flight_deck import basna_routes as b
from captain_claw.flight_deck import vatra_routes as vr

# ── action log ───────────────────────────────────────────────────────


def test_shell_command_shows_past_the_cd_prefix():
    cmd = ("cd /Users/someone/Dev/captain-claw/fd-data/vatra-7a8604f3-s4-data-analyst/"
           "data/workspace/saved/tmp/txt && python3 ../pg.py TWLO_10-K_FY2025.htm.txt '^Revenue'")
    out = b._summarize_tool_args({"command": cmd, "timeout": 600})
    assert out.startswith("command=python3 ../pg.py TWLO_10-K_FY2025.htm.txt")
    assert "fd-data" not in out
    assert "timeout=600" in out


def test_shell_command_chained_cds_and_heredoc_fold_to_one_line():
    cmd = 'cd "/a b/ws"; cd sub && cat > /tmp/x.py <<\'EOF\'\nimport re\nprint(1)\nEOF'
    out = b._summarize_tool_args({"command": cmd})
    assert out.startswith("command=cat > /tmp/x.py")
    assert "\n" not in out and "import re" in out


def test_bare_cd_is_kept():
    assert b._summarize_tool_args({"command": "cd /tmp"}) == "command=cd /tmp"


def test_folder_and_item_are_not_cut_and_four_args_show():
    folder = "/Users/someone/Dev/captain-claw/fd-data/vatra-x-s2/data/workspace"
    assert folder in b._summarize_tool_args({"folder": folder, "pattern": "*.pdf"})
    item = "Infobip Lenders Presentation_26082026.pdf"
    assert item in b._summarize_tool_args({"item": item, "step": "extract"})
    out = b._summarize_tool_args(
        {"step": "finished", "processed": 10, "failed": 6, "total": 16})
    assert "failed=6" in out and "total=16" in out


# ── extension policy ─────────────────────────────────────────────────


def test_unbounded_extends_past_the_ceiling_and_while_quiet():
    # Past the old hard ceiling, and quiet far longer than the activity window.
    new = b._extend_deadline(5000.0, 1800.0, 100.0, unbounded=True)
    assert new == 5000.0 + b._EXTEND_SLICE_S


def test_bounded_policy_unchanged():
    assert b._extend_deadline(1800.0, 1800.0, 1799.0) is None
    assert b._extend_deadline(600.0, 1800.0, 590.0) == 600.0 + b._EXTEND_SLICE_S


async def _fake_agent(turn_s: float, tick: float):
    """A websocket agent whose turn streams a tool event every `tick` seconds
    for `turn_s` seconds, then replies and goes idle."""
    async def handler(ws):
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.send(json.dumps({"type": "replay_done"}))
        async for raw in ws:
            if json.loads(raw).get("type") != "chat":
                continue
            loop = asyncio.get_running_loop()
            end = loop.time() + turn_s
            while loop.time() < end:
                await ws.send(json.dumps({"type": "monitor", "tool_name": "shell",
                                          "arguments": {"command": "echo hi"}}))
                await asyncio.sleep(tick)
            await ws.send(json.dumps({"type": "chat_message", "role": "assistant",
                                      "content": "all done"}))
            await ws.send(json.dumps({"type": "status", "status": "idle"}))
    return handler


@pytest.fixture()
def fast_slices(monkeypatch):
    monkeypatch.setattr(b, "_EXTEND_SLICE_S", 0.2)
    monkeypatch.setattr(b, "_EXTEND_ACTIVITY_S", 0.5)


async def _collect(port: int, timeout: float, unbounded: bool):
    err: dict = {}
    token = b._run_extend_unbounded.set(unbounded)
    try:
        out, actions = await b._send_chat_and_collect(
            port, "", "do it", timeout, error_sink=err)
    finally:
        b._run_extend_unbounded.reset(token)
    return out, actions, err


async def test_bounded_turn_is_cut_at_the_ceiling(fast_slices):
    from websockets.asyncio.server import serve

    # budget 0.3s → ceiling 0.9s (3×); the turn needs ~1.5s.
    async with serve(await _fake_agent(1.5, 0.05), "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        out, actions, err = await _collect(port, 0.3, unbounded=False)
    assert err.get("timed_out") is True
    assert out == ""
    assert any(a["tool"] == "dispatch_extend" and "cap" in a["detail"] for a in actions)


async def test_unbounded_turn_runs_to_the_end(fast_slices):
    from websockets.asyncio.server import serve

    async with serve(await _fake_agent(1.5, 0.05), "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        out, actions, err = await _collect(port, 0.3, unbounded=True)
    assert out == "all done"
    assert not err.get("timed_out")
    extends = [a["detail"] for a in actions if a["tool"] == "dispatch_extend"]
    assert len(extends) >= 3          # well past the old 3× ceiling
    assert all("no cap" in d for d in extends)


async def test_unbounded_keeps_waiting_through_a_quiet_stretch(fast_slices):
    """A long silent LLM call (no events past the activity window) is not cut."""
    from websockets.asyncio.server import serve

    async def handler(ws):
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.send(json.dumps({"type": "replay_done"}))
        async for raw in ws:
            if json.loads(raw).get("type") == "chat":
                await asyncio.sleep(1.2)  # > budget, > activity window, > 3× budget
                await ws.send(json.dumps({"type": "chat_message", "role": "assistant",
                                          "content": "late but whole"}))
                await ws.send(json.dumps({"type": "status", "status": "idle"}))

    async with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        out, actions, err = await _collect(port, 0.3, unbounded=True)
    assert out == "late but whole"
    assert any("quiet" in a["detail"] for a in actions if a["tool"] == "dispatch_extend")


# ── the run sets the flag (and only for itself) ─────────────────────


async def test_execute_vatra_runs_uncapped_and_resets(monkeypatch):
    seen: list[bool] = []

    async def inner(body, request, user):
        seen.append(b._run_extend_unbounded.get())
        # a child task created inside the run inherits it
        seen.append(await asyncio.create_task(_read_flag()))
        return {"ok": True}

    monkeypatch.setattr(vr, "_execute_vatra_inner", inner)
    assert b._run_extend_unbounded.get() is False
    await vr.execute_vatra(SimpleNamespace(session_id="s1"), None, {"id": "u1"})
    assert seen == [True, True]
    assert b._run_extend_unbounded.get() is False  # an inline caller doesn't inherit it


async def _read_flag() -> bool:
    return b._run_extend_unbounded.get()


async def test_group0_planner_runs_uncapped_and_resets(monkeypatch):
    seen: list[bool] = []

    async def inner(body, request, user, *, gate=True):
        seen.append(b._run_extend_unbounded.get())
        return {"ok": True}

    monkeypatch.setattr(vr, "_plan_vatra_group0_inner", inner)
    await vr.plan_vatra_group0(SimpleNamespace(session_id="s1"), None, {"id": "u1"})
    assert seen == [True]
    assert b._run_extend_unbounded.get() is False


# ── every spawned agent gets the reading kit ─────────────────────────


@pytest.fixture()
def spawn_capture(monkeypatch):
    from captain_claw.flight_deck import server as srv
    captured: dict = {}

    async def fake_spawn(cfg, request, user):
        captured["cfg"] = cfg
        return SimpleNamespace(ok=True, slug="w1", message="")

    monkeypatch.setattr(srv, "spawn_process", fake_spawn)
    monkeypatch.setattr(srv, "_load_process_registry",
                        lambda: {"w1": {"web_port": 12345, "web_auth": "tok"}})
    return captured


@pytest.mark.parametrize("tools", [
    ["web_search", "web_fetch", "datastore", "send_mail", "read", "write", "glob"],
    ["shell", "read", "write", "xlsx_extract", "datastore"],
    ["read", "ls"],
])
async def test_every_worker_gets_extract_and_search_tools(spawn_capture, tools):
    out = await vr._spawn_worker(
        object(), {"id": "u1"}, name="vatra-x-r1-t1-arch", description="d",
        cognitive_mode="neutra", tools=list(tools), tier="fast", tiers={},
        api_key="", env_vars=[])
    assert out["ok"]
    got = spawn_capture["cfg"].tools
    for t in vr._WORKER_EXTRACT_SEARCH_TOOLS:
        assert t in got, t
    assert got[:len(tools)] == list(tools)        # the archetype's own list leads
    assert len(got) == len(set(got))              # no duplicates
    assert "basna" not in got


async def test_basna_is_still_stripped(spawn_capture):
    await vr._spawn_worker(
        object(), {"id": "u1"}, name="n", description="d", cognitive_mode="neutra",
        tools=["basna", "read"], tier="fast", tiers={}, api_key="", env_vars=[])
    assert "basna" not in spawn_capture["cfg"].tools
