"""Vatra run state lives until the whole run ends, not just the team phase.

The inner team teardown used to pop the run's folder override, shared-datastore
binding, deliverable manifest and quality flags BEFORE the reporter, the
fact-checker and the done gate ran: a continuation round's reporter was pinned to
the wrong folder, and the Increment 4 assembly -> smooth -> gate path never ran.
These tests drive the real ``_execute_vatra_inner`` with every external edge
stubbed (spawn, dispatch, route, LLM digests, learning).
"""

import asyncio
import json
import types

import pytest
from fastapi import HTTPException

from captain_claw.flight_deck import deliverable_manifest as dm
from captain_claw.flight_deck import vatra_routes as vr


class _CatchAllDB:
    def __init__(self, sess):
        self.sess = sess
        self.updates = []

    async def get_basna_session(self, sid, uid):
        return dict(self.sess)

    async def update_basna_session(self, sid, uid, **kw):
        self.updates.append(kw)
        if "config" in kw:
            self.sess["config"] = kw["config"]

    async def add_basna_runs(self, sid, uid, rows):
        return [i for i, _ in enumerate(rows)]

    async def add_vatra_board(self, *a, **k):
        return {"id": 1}

    def __getattr__(self, name):
        async def _noop(*a, **k):
            return []
        return _noop


def _harness(monkeypatch, tmp_path, *, cfg, quality=None, deliverable=None,
             part_files=None, sid="abcdef0123456789"):
    import captain_claw.flight_deck.server as server
    monkeypatch.setattr(server, "DATA_DIR", tmp_path / "data", raising=False)
    vfs_root = tmp_path / "vfs"
    vfs_root.mkdir()
    from captain_claw.flight_deck import vfs_routes
    monkeypatch.setattr(vfs_routes, "_user_root", lambda uid: vfs_root)
    monkeypatch.setattr(vr, "_session_files_dir", lambda s: (tmp_path / "files"))
    (tmp_path / "files").mkdir()

    subtasks = [
        {"id": "s1", "title": "Opening chapters", "brief": "write", "owner_archetype_id": "writer"},
        {"id": "s2", "title": "Closing chapters", "brief": "write", "owner_archetype_id": "writer2"},
    ]
    route = {"domain": "fiction", "subtasks": subtasks, "shared_context": ""}
    sess = {"id": sid, "intent": "Write the story", "config": json.dumps(cfg),
            "route": json.dumps(route), "status": "running", "files": "[]"}
    db = _CatchAllDB(sess)
    monkeypatch.setattr(vr, "get_db", lambda: db)
    from captain_claw.flight_deck import auth
    monkeypatch.setattr(auth, "get_db", lambda: db)   # the run ledger's writes

    archs = [{"id": "writer", "role": "Writer", "tools": []},
             {"id": "writer2", "role": "Writer Two", "tools": []},
             {"id": "editor-writer", "role": "Editor", "tools": []}]

    async def _archs(db_, uid):
        return archs
    monkeypatch.setattr(vr, "merged_archetypes", _archs)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(vr, "_refresh_system_provider_keys", _noop)
    monkeypatch.setattr(vr, "_load_registry", lambda: {})
    monkeypatch.setattr(vr, "_resolve_creds", lambda *a, **k: {})

    async def _route(*a, **k):
        return json.loads(json.dumps(route))
    monkeypatch.setattr(vr, "_ensure_route", _route)
    for fn in ("_progress", "_progress_start", "_progress_done", "_phase"):
        monkeypatch.setattr(vr, fn, lambda *a, **k: None)
    monkeypatch.setattr(vr, "_teardown", lambda slugs: None)
    monkeypatch.setattr(vr, "_capture_generated", lambda *a, **k: ([], ""))

    spawns = {}

    async def _spawn(request, user, *, name, extra_env=None, **k):
        slug = name
        (tmp_path / "data" / slug / "data" / "workspace").mkdir(parents=True, exist_ok=True)
        spawns[name] = {e["key"]: e["value"] for e in (extra_env or [])}
        return {"ok": True, "slug": slug, "port": 1, "auth": "t", "message": ""}
    monkeypatch.setattr(vr, "_spawn_worker", _spawn)

    prompts = {}

    async def _dispatch(port, auth, prompt, timeout, **k):
        name = k.get("agent_name", "")
        prompts.setdefault(name, []).append(prompt)
        return {"ok": True, "output": f"# Chapter 1\n\nText by {name}. " + "z" * 400,
                "actions": [], "latency_ms": 1}
    monkeypatch.setattr(vr, "_dispatch_one", _dispatch)

    async def _coord(*a, **k):
        return 0
    monkeypatch.setattr(vr, "_coordinate_asks", _coord)

    async def _learn(*a, **k):
        return []
    monkeypatch.setattr(vr, "_learn", _learn)

    async def _cov(*a, **k):
        return None
    monkeypatch.setattr(vr, "_llm_coverage_gaps", _cov)

    async def _digest(*a, **k):
        return ""
    monkeypatch.setattr(vr, "_llm_team_digest", _digest)

    # the folder the run should land in
    proj = cfg.get("vfs_project") or f"vatra-{sid[:8]}"
    (vfs_root / proj).mkdir(parents=True, exist_ok=True)
    for name, text in (part_files or {}).items():
        (vfs_root / proj / name).write_text(text, encoding="utf-8")

    body = vr.ExecuteRequest(session_id=sid, quality=quality, deliverable=deliverable,
                             dispatch_timeout=30.0)
    user = {"id": "u1"}
    request = types.SimpleNamespace(state=types.SimpleNamespace(user_id="u1"))
    return body, request, user, spawns, prompts, db, vfs_root / proj

_MAPS = ("_run_vfs_project", "_run_shared_datastore", "_run_manifest", "_run_flags",
         "_run_workers", "_wait_ledger", "_skip_agents", "_owner_activity")


def _assert_released(sid):
    for name in _MAPS:
        assert sid not in getattr(vr, name), name


def _reporter(spawns):
    return next(v for k, v in spawns.items() if k.endswith("-reporter"))


_DECL = {"path": "the-story.md", "kind": "fiction",
         "parts": [{"path": "s1-part.md", "owner": "s1", "order": 1},
                   {"path": "s2-part.md", "owner": "s2", "order": 2}]}
_PARTS = {"s1-part.md": "# Chapter 1\n\nOpen. " + "a" * 300,
          "s2-part.md": "# Chapter 2\n\nClose. " + "b" * 300}


@pytest.mark.asyncio
async def test_continuation_reporter_root_folder_and_released(monkeypatch, tmp_path):
    cfg = {"mode": "vatra", "vfs_project": "vatra-root0001", "parent_session_id": "p",
           "review_round": False, "shared_datastore": True}
    body, req, user, spawns, prompts, db, _ = _harness(monkeypatch, tmp_path, cfg=cfg)
    seen = {}
    real = vr._run_reporter

    async def _spy(*a, **k):
        sid = body.session_id
        seen.update(folder=vr._vfs_project(sid), sched=sid in vr._group_schedule,
                    activity=sid in vr._owner_activity, flags=sid in vr._run_flags)
        return await real(*a, **k)
    monkeypatch.setattr(vr, "_run_reporter", _spy)
    res = await vr.execute_vatra(body, req, user)
    assert res.get("status") != "error", res
    assert (res.get("truth") or "").strip()
    assert seen == {"folder": "vatra-root0001", "sched": False, "activity": False, "flags": True}
    rep = _reporter(spawns)
    assert rep["CLAW_VFS_PROJECT"] == "vatra-root0001"
    assert rep["CLAW_DATASTORE_VFS"] == "vatra-root0001"
    assert "vfs:vatra-root0001/" in prompts["Editor"][0]
    _assert_released(body.session_id)


@pytest.mark.asyncio
async def test_declared_manifest_assembles_smooths_and_downloads(monkeypatch, tmp_path):
    cfg = {"mode": "vatra", "review_round": False, "parent_session_id": ""}
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg=cfg, quality={"profile": "long_form"},
        deliverable=_DECL, part_files=_PARTS)
    res = await vr.execute_vatra(body, req, user)
    assert "ALREADY assembled" in prompts["Editor"][0] or "already assembled" in prompts["Editor"][0].lower()
    assert res["truth"].startswith("# Chapter 1") and "# Chapter 2" in res["truth"]
    assert (folder / "the-story.md").read_text() == res["truth"]
    assert (tmp_path / "files" / "the-story.md").read_text() == res["truth"]
    assert res["analysis"]["deliverable"]["verdict"] == "ok"
    rep = _reporter(spawns)
    assert json.loads(rep["CLAW_DECLARED_FILES"]) == ["the-story.md"]
    assert "CLAW_WRITE_MIN_BYTES" not in rep and "CLAW_MY_ARTIFACT" not in rep
    _assert_released(body.session_id)


@pytest.mark.asyncio
async def test_declared_manifest_missing_part_blocks(monkeypatch, tmp_path):
    cfg = {"mode": "vatra", "review_round": False}
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg=cfg, quality={"profile": "long_form"},
        deliverable=_DECL, part_files={"s1-part.md": _PARTS["s1-part.md"]})
    res = await vr.execute_vatra(body, req, user)
    assert res.get("status") == "error", res
    assert "s2-part.md was never written" in json.dumps(db.updates)
    _assert_released(body.session_id)


@pytest.mark.asyncio
async def test_derived_manifest_keeps_rewrite_and_finishes(monkeypatch, tmp_path):
    cfg = {"mode": "vatra", "review_round": False}
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg=cfg, quality={"profile": "long_form"})
    res = await vr.execute_vatra(body, req, user)
    assert res.get("status") != "error", res
    assert "already assembled" not in prompts["Editor"][0].lower()
    assert res["analysis"]["deliverable"]["path"].endswith("write-the-story.md")
    assert not (folder / "write-the-story.md").exists()
    _assert_released(body.session_id)


@pytest.mark.asyncio
async def test_continuation_round_skips_concat(monkeypatch, tmp_path):
    cfg = {"mode": "vatra", "review_round": False, "parent_session_id": "p",
           "vfs_project": "vatra-root0001"}
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg=cfg, quality={"profile": "long_form"},
        deliverable=_DECL, part_files=_PARTS)
    res = await vr.execute_vatra(body, req, user)
    assert res.get("status") != "error", res
    assert "already assembled" not in prompts["Editor"][0].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["return", "http", "crash", "cancel"])
async def test_release_on_every_exit(monkeypatch, exit_kind):
    sid = f"exit-{exit_kind}"

    class _DB:
        async def get_basna_session(self, *a):
            return {"status": "running"}

        async def update_basna_session(self, *a, **k):
            pass
    monkeypatch.setattr(vr, "get_db", lambda: _DB())
    monkeypatch.setattr(vr, "_progress", lambda *a, **k: None)
    monkeypatch.setattr(vr, "_progress_done", lambda *a, **k: None)

    async def _inner(body, request, user):
        for name in _MAPS:
            getattr(vr, name)[sid] = {"x": 1} if name != "_run_vfs_project" else "vatra-x"
        if exit_kind == "http":
            raise HTTPException(502, "no usable output")
        if exit_kind == "crash":
            raise RuntimeError("late")
        if exit_kind == "cancel":
            raise asyncio.CancelledError()
        return {"ok": True}
    monkeypatch.setattr(vr, "_execute_vatra_inner", _inner)
    body = types.SimpleNamespace(session_id=sid)
    if exit_kind in ("http", "cancel"):
        with pytest.raises((HTTPException, asyncio.CancelledError)):
            await vr.execute_vatra(body, None, {"id": "u1"})
    else:
        await vr.execute_vatra(body, None, {"id": "u1"})
    _assert_released(sid)
    assert sid not in vr._run_owner


@pytest.mark.asyncio
async def test_stale_run_does_not_release_newer_run(monkeypatch):
    sid = "shared-sid"
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()

    async def _inner(body, request, user):
        vr._run_vfs_project[sid] = body.tag
        await (gate_a if body.tag == "A" else gate_b).wait()
        return {}
    monkeypatch.setattr(vr, "_execute_vatra_inner", _inner)
    ta = asyncio.create_task(vr.execute_vatra(types.SimpleNamespace(session_id=sid, tag="A"), None, {}))
    await asyncio.sleep(0)
    tb = asyncio.create_task(vr.execute_vatra(types.SimpleNamespace(session_id=sid, tag="B"), None, {}))
    await asyncio.sleep(0)
    gate_a.set()
    await ta
    assert vr._run_vfs_project.get(sid) == "B"  # A is stale: must not clear B's state
    gate_b.set()
    await tb
    assert sid not in vr._run_vfs_project and sid not in vr._run_owner


def test_role_env(monkeypatch):
    decl = dict(_DECL, min_bytes=60000,
                parts=_DECL["parts"] + [{"path": "r.md", "owner": "reporter", "order": 3,
                                         "min_bytes": 500}])
    m = dm.parse(decl, [{"id": "s1"}, {"id": "s2"}, {"id": "reporter"}], "vatra-p")
    assert m.part_for("reporter") is not None   # the guard below is what keeps it off
    sid = "role-env"
    monkeypatch.setitem(vr._run_flags, sid, {"write_guard": True})
    monkeypatch.setitem(vr._run_manifest, sid, m)
    fc = {e["key"]: e["value"] for e in vr._vatra_env(sid, "factcheck", "fact-checker", 0, role="factcheck")}
    assert "CLAW_WRITE_STRICT" not in fc and "CLAW_DECLARED_FILES" not in fc
    assert fc["CLAW_VFS_PROJECT"] == "vatra-role-env"
    rp = {e["key"]: e["value"] for e in vr._vatra_env(sid, "reporter", "editor-writer", 0, role="reporter")}
    assert rp["CLAW_WRITE_STRICT"] == "1"
    assert json.loads(rp["CLAW_DECLARED_FILES"]) == ["the-story.md"]
    assert "CLAW_WRITE_MIN_BYTES" not in rp and "CLAW_MY_ARTIFACT" not in rp  # even with a subtask "reporter"
    w = {e["key"]: e["value"] for e in vr._vatra_env(sid, "s1", "writer", 0)}
    assert w["CLAW_MY_ARTIFACT"] == "s1-part.md" and w["CLAW_WRITE_MIN_BYTES"] == "60000"


# ── review follow-ups ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concat_then_smooth_alone_still_assembles(monkeypatch, tmp_path):
    """No research map / facts / git / contract: the run folder is still resolved."""
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"synthesis_emit": "concat_then_smooth", "write_guard": True},
        deliverable=_DECL, part_files=_PARTS)
    res = await vr.execute_vatra(body, req, user)
    assert "already assembled" in prompts["Editor"][0].lower()
    assert (folder / "the-story.md").read_text() == res["truth"]


def _stub_gate(monkeypatch, revise):
    seen = {}

    async def _run_gate(text, **kw):
        seen["file_before"] = (seen["dl"] / "the-story.md").read_text()
        seen["text"] = text
        return {"revised": True, "text": revise(text), "verdict": "clean", "rounds": 1,
                "remaining": []}
    monkeypatch.setattr(vr.quality_findings, "run_gate", _run_gate)
    return seen


@pytest.mark.asyncio
async def test_a_gate_revision_reaches_both_deliverable_copies(monkeypatch, tmp_path):
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"profile": "long_form", "block_on_critical": True},
        deliverable=_DECL, part_files=_PARTS)
    seen = _stub_gate(monkeypatch, lambda t: t + "\n\nRevised ending.")
    seen["dl"] = tmp_path / "files"
    res = await vr.execute_vatra(body, req, user)
    assert seen["file_before"] == seen["text"]          # synced after the reporter already
    assert res["truth"].endswith("Revised ending.")
    assert (folder / "the-story.md").read_text() == res["truth"]
    assert (tmp_path / "files" / "the-story.md").read_text() == res["truth"]


@pytest.mark.asyncio
async def test_a_revision_that_cuts_the_assembly_is_rejected(monkeypatch, tmp_path):
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"profile": "long_form", "block_on_critical": True},
        deliverable=_DECL, part_files=_PARTS)
    seen = _stub_gate(monkeypatch, lambda t: t[: len(t) // 2])   # chapter 2 lost
    seen["dl"] = tmp_path / "files"
    res = await vr.execute_vatra(body, req, user)
    assert "# Chapter 2" in res["truth"]
    assert (folder / "the-story.md").read_text() == res["truth"]


@pytest.mark.asyncio
async def test_parts_without_owners_keep_the_reporter_rewrite(monkeypatch, tmp_path):
    unowned = {"path": "the-story.md", "kind": "fiction",
               "parts": [{"path": "part-one.md", "range": "ch1-5"},
                         {"path": "part-two.md", "range": "ch6-10"}]}
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"profile": "long_form"}, deliverable=unowned)
    res = await vr.execute_vatra(body, req, user)
    assert res.get("status") != "error", res
    assert "already assembled" not in prompts["Editor"][0].lower()


@pytest.mark.asyncio
async def test_an_upload_with_the_deliverables_name_is_kept(monkeypatch, tmp_path):
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"profile": "long_form"}, deliverable=_DECL, part_files=_PARTS)
    (tmp_path / "files" / "the-story.md").write_text("my original draft")
    sess = db.sess
    sess["files"] = json.dumps([{"name": "the-story.md", "kind": "input", "mime": "text/markdown"}])
    res = await vr.execute_vatra(body, req, user)
    assert (tmp_path / "files" / "the-story.md").read_text() == "my original draft"
    assert (tmp_path / "files" / "assembly__the-story.md").read_text() == res["truth"]
    persisted = [json.loads(u["files"]) for u in db.updates if u.get("files")][-1]
    names = {f["name"]: f.get("kind") for f in persisted}
    assert names.get("the-story.md") == "input" and names.get("assembly__the-story.md") == "generated"


_DUP = {"s1-part.md": "# Part One\n\n## Chapter 1\n\n" + "a" * 300 + "\n\n## Chapter 2\n\nx",
        "s2-part.md": "# Part Two\n\n## Chapter 2\n\n" + "b" * 300}        # a real duplicate


@pytest.mark.asyncio
async def test_chapter_heuristics_only_warn_without_a_strict_gate(monkeypatch, tmp_path):
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"synthesis_emit": "concat_then_smooth"}, deliverable=_DECL, part_files=_DUP)
    res = await vr.execute_vatra(body, req, user)
    assert res.get("status") != "error", res
    dl = res["analysis"]["deliverable"]
    assert dl["verdict"] == "ok"
    assert [f["kind"] for f in dl["assembly_warnings"]] == ["duplicate_chapter"]


@pytest.mark.asyncio
async def test_a_strict_gate_blocks_a_real_duplicate_but_not_part_headings(monkeypatch, tmp_path):
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"profile": "long_form"}, deliverable=_DECL, part_files=_DUP)
    res = await vr.execute_vatra(body, req, user)
    assert res.get("status") == "error" and "chapter 2 appears in both" in res.get("blocked", "")
    # continuous chapter numbering under "Part N" headings is not a duplicate
    (folder / "s2-part.md").write_text("# Part Two\n\n## Chapter 3\n\n" + "b" * 300)
    res2 = await vr.execute_vatra(body, req, user)
    assert res2.get("status") != "error", res2


@pytest.mark.asyncio
async def test_a_stale_run_leaves_the_newer_runs_team_state_alone(monkeypatch, tmp_path):
    """Stop → Resume while the old run is still unwinding: the old run's team
    teardown must not clear the resumed run's workers / liveness / schedule."""
    import contextvars

    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False})
    who = contextvars.ContextVar("who", default="")
    gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    blocked = {"A": asyncio.Event(), "B": asyncio.Event()}
    real_dispatch = vr._dispatch_one

    async def _dispatch(port, auth, prompt, timeout, **k):
        run = who.get()
        if run and not gates[run].is_set():
            blocked[run].set()
            await gates[run].wait()
        return await real_dispatch(port, auth, prompt, timeout, **k)
    monkeypatch.setattr(vr, "_dispatch_one", _dispatch)

    async def _run(tag):
        who.set(tag)
        return await vr.execute_vatra(body, req, user)

    ta = asyncio.create_task(_run("A"))
    await blocked["A"].wait()
    tb = asyncio.create_task(_run("B"))
    await blocked["B"].wait()
    b_workers = list(vr._run_workers.get(body.session_id) or [])
    assert b_workers
    gates["A"].set()
    await ta                                          # A unwinds while B is mid-team
    assert vr._run_workers.get(body.session_id) == b_workers
    assert body.session_id in vr._run_owner
    gates["B"].set()
    res_b = await tb
    assert res_b.get("status") != "error", res_b
    _assert_released(body.session_id)
    assert body.session_id not in vr._run_owner


@pytest.mark.asyncio
async def test_run_tasks_are_registered_for_stop_and_a_second_approve_is_refused(monkeypatch):
    from captain_claw.flight_deck.basna_routes import _agent_run_tasks

    sid = "approve-twice"
    blocker = asyncio.Event()

    class _DB:
        async def get_basna_session(self, *a):
            return {"status": "awaiting_plan", "route": "{}", "config": "{}"}

    async def _no_tiers(db, uid):
        return {}, []
    monkeypatch.setattr(vr, "get_db", lambda: _DB())
    monkeypatch.setattr(vr, "_load_owner_tiers", _no_tiers)

    async def _live():
        await blocker.wait()
    t = asyncio.create_task(_live())
    vr._track_run_task(sid, t)          # what approve / continuation / resume now do
    assert _agent_run_tasks.get(sid) is t   # so Stop (_cancel_basna_run) cancels it
    with pytest.raises(HTTPException) as err:
        await vr.approve_vatra_plan(vr.VatraPlanApproveRequest(session_id=sid), None, {"id": "u1"})
    assert err.value.status_code == 409
    blocker.set()
    await t
    await asyncio.sleep(0)
    assert sid not in _agent_run_tasks


# ── review pass 2 ────────────────────────────────────────────────────


def _capture_progress(monkeypatch):
    events = []
    monkeypatch.setattr(vr, "_progress", lambda sid, kind, msg="", **k: events.append((kind, msg, k)))
    return events


@pytest.mark.asyncio
async def test_a_rejected_revision_reports_the_shipped_texts_findings(monkeypatch, tmp_path):
    """A canon pass whose fix is rejected must not leave its own (clean) findings
    behind: the original's critical contradiction still blocks `done`."""
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"profile": "long_form", "canon_pass": True, "gate_blocks_done": True},
        deliverable=_DECL, part_files=_PARTS)
    crit = [{"kind": "contradiction", "severity": "critical", "detail": "the date moves"}]

    async def _canon(text, **kw):
        return {"text": text[: len(text) // 2], "revised": True, "findings": [],
                "initial_findings": crit, "chunks": 1, "patched": 1, "unapplied": 0}
    monkeypatch.setattr(vr.research_canon, "run_check", _canon)
    res = await vr.execute_vatra(body, req, user)
    assert "# Chapter 2" in res["truth"]                       # the cut was rejected
    assert res.get("status") == "error" and "canon contradiction" in res.get("blocked", "")


@pytest.mark.asyncio
async def test_a_fact_check_rewrite_cannot_cut_the_assembly(monkeypatch, tmp_path):
    from captain_claw.flight_deck import research_verify as rv

    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"profile": "long_form", "claim_check": True}, deliverable=_DECL, part_files=_PARTS)
    monkeypatch.setattr(rv, "parse_findings", lambda out: [])
    monkeypatch.setattr(rv, "fix_instructions", lambda f: "replace X with Y")
    events = _capture_progress(monkeypatch)
    res = await vr.execute_vatra(body, req, user)
    assert res["truth"].startswith("# Chapter 1") and "# Chapter 2" in res["truth"]
    assert (folder / "the-story.md").read_text() == res["truth"]
    assert any("would cut the assembled deliverable" in m for _, m, _k in events)


@pytest.mark.asyncio
async def test_removing_a_duplicated_chapter_is_allowed(monkeypatch, tmp_path):
    parts = {"s1-part.md": "## Chapter 1\n\n" + "a" * 300,
             "s2-part.md": "## Chapter 2\n\n" + "b" * 300 + "\n\n## Chapter 2\n\n" + "b" * 20}
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"synthesis_emit": "concat_then_smooth", "block_on_critical": True},
        deliverable=_DECL, part_files=parts)
    seen = _stub_gate(monkeypatch, lambda t: t[: t.rindex("## Chapter 2")].rstrip())
    seen["dl"] = tmp_path / "files"
    res = await vr.execute_vatra(body, req, user)
    assert res["truth"].count("## Chapter 2") == 1


@pytest.mark.asyncio
async def test_small_cuts_do_not_add_up(monkeypatch, tmp_path):
    """Each pass may trim < 10%, but not the deliverable as a whole."""
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"profile": "long_form", "consistency_check": True, "block_on_critical": True},
        deliverable=_DECL, part_files=_PARTS)

    def _trim(t):
        return t[: int(len(t) * 0.93)]

    async def _consistency(text, **kw):
        return {"text": _trim(text), "revised": True, "findings": [], "initial_findings": [],
                "checked": {"values": 0, "relations": 0}, "truncated": False}
    monkeypatch.setattr(vr.research_consistency, "run_check", _consistency)
    seen = _stub_gate(monkeypatch, _trim)
    seen["dl"] = tmp_path / "files"
    res = await vr.execute_vatra(body, req, user)
    assembled = len(_PARTS["s1-part.md"]) + len(_PARTS["s2-part.md"]) + 2
    assert len(res["truth"]) >= 0.9 * assembled


@pytest.mark.asyncio
async def test_chapter_warnings_are_shown(monkeypatch, tmp_path):
    body, req, user, spawns, prompts, db, folder = _harness(
        monkeypatch, tmp_path, cfg={"mode": "vatra", "review_round": False},
        quality={"synthesis_emit": "concat_then_smooth"}, deliverable=_DECL, part_files=_DUP)
    events = _capture_progress(monkeypatch)
    await vr.execute_vatra(body, req, user)
    assert any("1 warning(s)" in m and "chapter 2 appears in both" in m for _, m, _k in events)


# ── lifecycle: Stop / Resume / approve ───────────────────────────────


class _StatusDB:
    def __init__(self, status, **extra):
        self.sess = {"status": status, "route": "{}", "config": "{}", **extra}
        self.updates = []

    async def get_basna_session(self, *a):
        await asyncio.sleep(0)
        return dict(self.sess)

    async def update_basna_session(self, sid, uid, **kw):
        await asyncio.sleep(0)
        self.updates.append(kw)
        self.sess.update({k: v for k, v in kw.items() if k == "status"})


@pytest.mark.asyncio
@pytest.mark.parametrize("prior,after", [("planning", "routed"), ("running", "cancelled")])
async def test_stop_while_planning_returns_to_the_team_plan(monkeypatch, prior, after):
    from captain_claw.flight_deck import basna_routes as b

    db = _StatusDB(prior)
    monkeypatch.setattr(b, "get_db", lambda: db)
    await b._cancel_basna_run("stop-me", "u1")
    assert db.sess["status"] == after


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["planning", "routed", "awaiting_plan"])
async def test_resume_refuses_a_run_whose_team_never_started(monkeypatch, status):
    monkeypatch.setattr(vr, "get_db", lambda: _StatusDB(status))
    with pytest.raises(HTTPException) as err:
        await vr.launch_vatra_resume("never-ran", {"id": "u1"})
    assert err.value.status_code == 400


@pytest.mark.asyncio
async def test_two_approves_start_one_team_and_write_one_plan(monkeypatch):
    sid = "approve-race"
    db = _StatusDB("awaiting_plan", route=json.dumps({"subtasks": [{"id": "s1"}]}))
    monkeypatch.setattr(vr, "get_db", lambda: db)

    async def _no_tiers(d, uid):
        return {}, []
    monkeypatch.setattr(vr, "_load_owner_tiers", _no_tiers)
    hold = asyncio.Event()

    async def _run(*a, **k):
        await hold.wait()
        return {}
    monkeypatch.setattr(vr, "execute_vatra", _run)
    plan = {"agents": [{"subtask_id": "s1", "mandate": "m"}]}
    results = await asyncio.gather(
        *[vr.approve_vatra_plan(vr.VatraPlanApproveRequest(session_id=sid, plan=plan), None, {"id": "u1"})
          for _ in range(2)], return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert [r.status_code for r in results if isinstance(r, HTTPException)] == [409]
    assert sum(1 for u in db.updates if "route" in u) == 1
    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_execute_is_refused_over_a_live_run(monkeypatch):
    sid = "live-run"
    monkeypatch.setattr(vr, "get_db", lambda: _StatusDB("running"))
    blocker = asyncio.Event()

    async def _live():
        await blocker.wait()
    t = asyncio.create_task(_live())
    vr._track_run_task(sid, t)
    with pytest.raises(HTTPException) as err:
        await vr.execute_vatra_ui(vr.VatraExecuteRequest(session_id=sid), None, {"id": "u1"})
    assert err.value.status_code == 409
    blocker.set()
    await t


@pytest.mark.asyncio
async def test_an_old_task_finishing_late_keeps_the_new_registration():
    from captain_claw.flight_deck.basna_routes import _agent_run_tasks

    sid = "late-old"
    old_gate, new_gate = asyncio.Event(), asyncio.Event()

    async def _wait(ev):
        await ev.wait()
    old = asyncio.create_task(_wait(old_gate))
    vr._track_run_task(sid, old)
    new = asyncio.create_task(_wait(new_gate))
    vr._track_run_task(sid, new)
    old_gate.set()
    await old
    await asyncio.sleep(0)
    assert _agent_run_tasks.get(sid) is new
    new_gate.set()
    await new
    await asyncio.sleep(0)
    assert sid not in _agent_run_tasks
