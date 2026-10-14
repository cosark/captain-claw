"""Autonomous Work and mailbox organizing (archive / move / labels / categories).

The agent's google_mail / microsoft_mail organize actions are a separate class
from email writes (mail_authority.MAILBOX_CHANGE_ACTIONS). On the FD side:

* a custom action whose fixed action organizes mail is ``human_only`` and
  ``proposable`` — it may be proposed, never auto-fired, and runs under
  ``allow`` only after the user's Approve (unapproved, ``deny``: the agent
  refuses it);
* a google_mail / microsoft_mail spec whose args may set ``action`` is a mail
  write — ``build_tool_call`` lets an arg override the fixed action;
* every autonomy chat turn's no-mail line forbids reorganizing mail too.
"""

from __future__ import annotations

import pytest

from captain_claw.config import AutonomousWorkConfig
from captain_claw.flight_deck import action_catalog, actions, autonomy, fd_dispatch

UID = "user-alice"

_ORGANIZE = [
    ("google_mail", "archive"), ("google_mail", "move"), ("google_mail", "add_labels"),
    ("google_mail", "remove_labels"), ("google_mail", "create_label"),
    ("microsoft_mail", "archive"), ("microsoft_mail", "move"),
    ("microsoft_mail", "create_folder"), ("microsoft_mail", "add_categories"),
    ("microsoft_mail", "remove_categories"),
]


@pytest.fixture(autouse=True)
def _isolated_fd_data(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_DATA_DIR", str(tmp_path / "fd-data"))
    from captain_claw.flight_deck import consciousness, events, fd_scheduler, plans
    for mod in (autonomy, events, plans, consciousness, fd_scheduler):
        monkeypatch.setattr(mod, "_STORE", None, raising=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = autonomy.AutonomyStore(tmp_path / "autonomy.db")
    monkeypatch.setattr(autonomy, "_STORE", s)
    monkeypatch.setattr(autonomy, "global_defaults", lambda: AutonomousWorkConfig().model_dump())
    return s


def _custom(aid: str, tool: str, action: str, **over) -> dict:
    return {"id": aid, "label": aid, "tool": tool, "base_args": {"action": action},
            "required": ["thread_ids"], "human_only": False, "risk": "low",
            "reversibility": "reversible", **over}


def test_the_mirror_matches_the_agents_set():
    from captain_claw import mail_authority

    assert action_catalog.MAIL_ORGANIZE_ACTIONS == mail_authority.MAILBOX_CHANGE_ACTIONS
    assert not action_catalog.MAIL_ORGANIZE_ACTIONS & action_catalog.MAIL_WRITE_ACTIONS


@pytest.mark.parametrize("tool,action", _ORGANIZE)
def test_organize_specs_are_organize_not_mail_writes(tool, action):
    spec = {"tool": tool, "base_args": {"action": action}, "required": ["thread_ids"]}
    assert action_catalog.is_mail_organize(spec)
    assert not action_catalog.is_mail_write(spec)


def test_what_is_not_an_organize_spec():
    for spec in (
        None,
        {"tool": "google_mail", "base_args": {"action": "list_messages"}},
        {"tool": "google_mail", "base_args": {"action": "create_draft"}},
        {"tool": "google_mail"},                                    # action from args → a write
        {"tool": "google_calendar", "base_args": {"action": "move"}},
        {"tool": "mcp_gmail_label_thread", "base_args": {"action": "archive"}},
        action_catalog.get_action("calendar.hold"),
    ):
        assert not action_catalog.is_mail_organize(spec), spec


@pytest.mark.parametrize("key", ["required", "optional"])
@pytest.mark.parametrize("tool", ["google_mail", "microsoft_mail"])
def test_a_spec_whose_args_may_set_the_action_is_a_mail_write(tool, key):
    spec = {"tool": tool, "base_args": {"action": "archive"}, key: ["action", "thread_ids"]}
    assert action_catalog.is_mail_write(spec)
    assert not action_catalog.is_mail_organize(spec)
    # …because an arg overrides the fixed action:
    _tool, args = action_catalog.build_tool_call(spec, {"action": "send", "thread_ids": ["t1"]})
    assert args["action"] == "send"
    # Other tools are judged as before.
    assert not action_catalog.is_mail_write(
        {"tool": "google_calendar", "base_args": {"action": "create_event"}, key: ["action"]})


@pytest.mark.parametrize("tool,action", _ORGANIZE)
def test_custom_organize_action_is_human_only_but_proposable(store, tool, action):
    store.set_overrides(UID, {"custom_actions": [_custom("my.tidy", tool, action)]})
    spec = action_catalog.resolve_catalog(UID)["my.tidy"]
    assert spec["human_only"] is True and spec["proposable"] is True
    assert action_catalog.may_propose(spec) is True
    (row,) = [a for a in action_catalog.list_catalog(user_id=UID) if a["id"] == "my.tidy"]
    assert row["human_only"] is True and row["proposable"] is True
    assert row["mail_write"] is False


def test_custom_organize_action_with_an_overridable_action_is_never_proposed(store):
    store.set_overrides(UID, {"custom_actions": [
        _custom("my.tidy", "google_mail", "archive", optional=["action"]),
    ]})
    spec = action_catalog.resolve_catalog(UID)["my.tidy"]
    assert action_catalog.is_mail_write(spec)
    assert spec["human_only"] is True and not spec.get("proposable")
    assert action_catalog.may_propose(spec) is False


def test_other_custom_actions_keep_their_flags(store):
    store.set_overrides(UID, {"custom_actions": [
        _custom("my.read", "google_mail", "list_messages", required=[]),
        _custom("my.hold", "google_calendar", "create_event", required=[]),
    ]})
    cat = action_catalog.resolve_catalog(UID)
    for aid in ("my.read", "my.hold"):
        assert cat[aid]["human_only"] is False and not cat[aid].get("proposable"), aid


def test_a_granted_trusted_organize_action_never_auto_fires(store):
    store.set_overrides(UID, {"custom_actions": [_custom("my.tidy", "google_mail", "archive")]})
    with store._lock:
        store._c().execute(
            "INSERT OR REPLACE INTO autonomy_reliability"
            " (user_id, kind, domain, successes, fails, runs, weight, updated_at)"
            " VALUES (?, 'tool_action', 'my.tidy', 49, 0, 49, 0.97, '2026-01-01T00:00:00+00:00')",
            (UID,))
        store._c().commit()
    cfg = AutonomousWorkConfig().model_dump()
    cfg.update({"enabled": True, "allow_auto_dispatch": True, "autonomy_level": "act",
                "granted_actions": ["my.tidy", "custom"]})
    row = {"kind": "tool_action", "user_id": UID, "payload": {"action_id": "my.tidy"}}
    assert fd_dispatch.should_auto_dispatch(cfg, row) is False


@pytest.mark.parametrize("approved,mode", [(False, "deny"), (True, "allow")])
async def test_an_organize_action_runs_under_allow_only_when_approved(
        store, monkeypatch, approved, mode):
    store.set_overrides(UID, {"custom_actions": [_custom("my.tidy", "microsoft_mail", "archive")]})
    sent: list = []

    async def fake_run_tool(agent, tool, args, timeout=60.0, *, automation=None):
        sent.append((tool, args, automation))
        return {"ok": False, "content": "", "error": "x"}

    monkeypatch.setattr(actions, "run_tool_on_agent", fake_run_tool)
    monkeypatch.setattr(fd_dispatch, "_strongest_agent",
                        lambda uid: {"host": "localhost", "port": 1, "auth": ""})
    await actions.run_action(UID, "my.tidy", {"thread_ids": ["C1"]}, approved_by_human=approved)
    ((tool, args, automation),) = sent
    assert tool == "microsoft_mail" and args == {"action": "archive", "thread_ids": ["C1"]}
    assert automation == {"kind": "autonomy_tool", "job_text": "", "mail_write": mode}


def test_the_no_mail_line_forbids_reorganizing_mail():
    line = fd_dispatch._NO_MAIL_LINE
    assert "Do not create, update or send any email or email draft in this turn" in line
    assert "do not archive, move, label or otherwise reorganize any email" in line
    assert "offer to draft it" in line
