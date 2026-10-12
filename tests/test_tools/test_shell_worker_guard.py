"""Vatra worker shell rules: no web page dumped into the context, no writes into
VFS storage outside the run's folder. Every other agent is untouched.

The commands are the shapes a real Vatra run produced (Market Scanner, Data
Analyst, Deep Researcher), with the paths moved under a temp VFS root.
"""

from __future__ import annotations

import json

import pytest

from captain_claw.tools.shell_worker_guard import PAGE_DUMP_MESSAGE, worker_shell_refusal

PROJECT = "vatra-7a8604f3"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    vfs_root = tmp_path / "fd-data" / "vfs"
    user_root = vfs_root / "u1"
    run = user_root / PROJECT
    (run / "peer_reports" / "ERIC-B").mkdir(parents=True)
    (user_root / "other-project").mkdir(parents=True)
    (vfs_root / "someone-else" / "their-project").mkdir(parents=True)
    ws = tmp_path / "fd-data" / "vatra-7a8604f3-s4-data-analyst" / "data" / "workspace"
    (ws / "saved" / "tmp").mkdir(parents=True)
    monkeypatch.setenv("CLAW_VATRA_WORKER", "1")
    monkeypatch.setenv("CLAW_VFS_ROOT", str(vfs_root))
    monkeypatch.setenv("CLAW_VFS_USER", "u1")
    monkeypatch.setenv("CLAW_VFS_PROJECT", PROJECT)
    return {"vfs": vfs_root, "user": user_root, "run": run, "ws": ws, "tmp": tmp_path}


def _check(env, command):
    return worker_shell_refusal(command, env["ws"])


# ── not a Vatra worker → never refused ───────────────────────────────


def test_other_agents_are_untouched(env, monkeypatch):
    monkeypatch.delenv("CLAW_VATRA_WORKER")
    assert _check(env, "curl -s https://example.com") is None
    assert _check(env, f"touch {env['user']}/other-project/x") is None


# ── rule 1: pages into the context ───────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "curl -s https://investors.sinch.com/reports-and-presentations",
    'cd /tmp; curl -s -m 30 -L -A "Mozilla/5.0" https://www.cxtoday.com/gartner',
    "wget -qO- https://example.com/page",
    "wget -O - https://example.com/page",
    'UA="Crowe research analyst"; curl -s -A "$UA" "https://www.sec.gov/x/index.htm"',
])
def test_page_dump_is_refused(env, cmd):
    assert _check(env, cmd) == PAGE_DUMP_MESSAGE


@pytest.mark.parametrize("cmd", [
    # downloads to a file (the run's folder or the workspace)
    'cd {run}/peer_reports/ERIC-B && curl -s -A "UA" -o ERIC_AR.htm https://www.sec.gov/a.htm',
    "curl -sL https://example.com/r.pdf -o saved/downloads/r.pdf",
    "cd saved/tmp && curl -sLO https://example.com/r.pdf",
    "wget https://example.com/r.pdf",
    "curl -s https://example.com/x > saved/tmp/x.html",
    # filtered through a pipe
    "curl -s https://www.sec.gov/index.htm | grep -o 'href=\"/Archives[^\"]*\"' | head",
    # headers only
    "curl -sI https://example.com",
    # a URL mentioned in another command
    "echo https://example.com >> saved/tmp/sources.md",
])
def test_downloads_pipes_and_headers_are_fine(env, cmd):
    assert _check(env, cmd.format(run=env["run"])) is None


# ── rule 2: VFS storage outside the run's folder ─────────────────────


@pytest.mark.parametrize("cmd", [
    "cd {run} && mkdir -p peer_reports/TWLO && cp {ws}/saved/tmp/a.pdf peer_reports/TWLO/",
    "cd {run}/peer_reports/ERIC-B && wget https://example.com/ex15.htm",
    "unzip -o saved/tmp/reports.zip -d {run}/peer_reports",
    "cat > {run}/notes.md <<'EOF'\nline > other\nEOF",
    "W={run}; python3 conv.py > $W/peer_reports/index.txt",
    "cd {ws}/saved/tmp && cat > /tmp/conv.py <<'EOF'\nbase=\"{vfs}/u1/other-project\"\nEOF",
    "grep -n Revenue {user}/other-project/a.txt 2>/dev/null | head",   # reading is fine
    "unzip -l {user}/other-project/a.zip",                               # listing writes nothing
])
def test_run_folder_workspace_and_reads_are_allowed(env, cmd):
    assert _check(env, cmd.format(run=env["run"], ws=env["ws"], vfs=env["vfs"],
                                  user=env["user"])) is None


@pytest.mark.parametrize("cmd", [
    "touch {user}/other-project/x.md",
    "cd {user}/other-project && curl -s -o r.pdf https://example.com/r.pdf",
    "cd {vfs}/someone-else/their-project; wget https://example.com/r.pdf",
    "echo hi >> {vfs}/someone-else/their-project/notes.md",
    "W={vfs}/someone-else; cp saved/tmp/a.pdf $W/their-project/",
    "rm -rf {user}/other-project",
    "mv {run}/a.md {user}/other-project/a.md",
    "cd {run} && tar -xzf {ws}/saved/tmp/x.tgz -C ../other-project",
])
def test_writes_elsewhere_in_vfs_storage_are_refused(env, cmd):
    out = _check(env, cmd.format(run=env["run"], ws=env["ws"], vfs=env["vfs"],
                                 user=env["user"]))
    assert out is not None and "outside this run's folder" in out
    assert f"vfs:{PROJECT}/" in out


def test_linked_folders_other_than_the_run_are_protected(env):
    linked = env["tmp"] / "elsewhere" / "linked"
    linked.mkdir(parents=True)
    (env["user"] / ".vfs-links.json").write_text(
        json.dumps({"docs": {"path": str(linked), "mode": "ro"}}))
    out = _check(env, f"cp saved/tmp/a.pdf {linked}/a.pdf")
    assert out is not None and "outside this run's folder" in out
    assert _check(env, f"cat {linked}/a.txt") is None


def test_a_read_only_run_folder_refuses_writes(env):
    linked = env["tmp"] / "ro-run"
    linked.mkdir()
    (env["user"] / ".vfs-links.json").write_text(
        json.dumps({PROJECT: {"path": str(linked), "mode": "ro"}}))
    out = _check(env, f"touch {linked}/x")
    assert out is not None and "read-only" in out


def test_unparseable_commands_pass_through(env):
    assert _check(env, "echo 'unterminated") is None
