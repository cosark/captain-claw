"""google_mail organizing: archive / move / add_labels / remove_labels /
create_label.

Label changes only (gmail.modify): whole conversations go through
``threads.modify`` (one each), single messages through ONE
``messages.batchModify`` (retried one by one to isolate a bad ID). Label names
resolve through one ``labels.list`` per call; an unknown or out-of-scope label
(trash, spam, sent…) refuses the whole call before anything changes. A missing
gmail.modify scope refuses before any Gmail call, worded for Flight Deck or
standalone. The automated-turn guard itself is pinned in
tests/test_mail_authority_wiring.py.

No network: Gmail answers through httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

import captain_claw.session as session_mod
from captain_claw import mail_authority as ma
from captain_claw.google_oauth import GoogleOAuthTokens
from captain_claw.google_oauth_manager import GoogleOAuthManager
from captain_claw.tools import google_mail as gm
from captain_claw.tools.google_mail import GoogleMailTool

_G = "https://www.googleapis.com/auth/"
READ = _G + "gmail.readonly"
COMPOSE = _G + "gmail.compose"
MODIFY = _G + "gmail.modify"


# ── fixtures (copied from tests/test_gmail_send_tool.py) ─────────────


@pytest.fixture(autouse=True)
def _no_real_session_manager(monkeypatch):
    # The tool builds GoogleOAuthManager(get_session_manager()); never touch a DB.
    monkeypatch.setattr(session_mod, "get_session_manager", lambda: object())


@pytest.fixture(autouse=True)
def _isolated_fd_home(monkeypatch, tmp_path):
    from captain_claw.flight_deck import agent_secret

    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    agent_secret.reset_cache_for_tests()
    yield
    agent_secret.reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _human_turn(monkeypatch):
    """A human turn — no FD-worker process default and no bound automation."""
    for name in ("CLAW_BEING_WORKER", "CLAW_BASNA_WORKER", "CLAW_VATRA_WORKER",
                 "CLAW_COUNCIL_WORKER", "CLAW_CODE_AGENT"):
        monkeypatch.delenv(name, raising=False)
    assert ma.current() == ma.HUMAN


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    import captain_claw.config as config_mod
    from captain_claw.config import Config

    cfg = Config()
    monkeypatch.setattr(config_mod, "_config", cfg)
    return cfg


def _set_mode(monkeypatch, *, fd: bool = False, scope: str = f"{READ} {MODIFY}") -> dict:
    """Flight Deck client mode on/off + the token (and its scopes) the manager hands out."""
    calls = {"get_tokens": 0}
    monkeypatch.setattr(
        GoogleOAuthManager, "_flight_deck_base",
        staticmethod(lambda: "http://localhost:25080" if fd else ""),
    )

    async def _get_tokens(self):
        calls["get_tokens"] += 1
        return GoogleOAuthTokens(access_token="T", refresh_token="",
                                 expires_at=time.time() + 3300, scope=scope)

    monkeypatch.setattr(GoogleOAuthManager, "get_tokens", _get_tokens)
    return calls


class _Gmail:
    """Scripted Gmail API: (method, path suffix) → (status, JSON or None for an
    empty body), or a callable taking the request; records requests."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("/gmail/v1/users/me", 1)[-1]
        route = self.routes.get((request.method, path), (404, {"error": {"message": "nope"}}))
        status, body = route(request) if callable(route) else route
        return httpx.Response(status) if body is None else httpx.Response(status, json=body)

    def calls(self, method: str = "POST") -> list[tuple[str, dict | None]]:
        """(path, JSON body) of each *method* request, in order."""
        return [
            (r.url.path.split("/gmail/v1/users/me", 1)[-1], json.loads(r.content) if r.content else None)
            for r in self.requests if r.method == method
        ]


def _tool_with(gmail: _Gmail) -> GoogleMailTool:
    tool = GoogleMailTool()
    tool._client = httpx.AsyncClient(transport=httpx.MockTransport(gmail))
    return tool


LABELS = (200, {"labels": [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "UNREAD", "name": "UNREAD", "type": "system"},
    {"id": "STARRED", "name": "STARRED", "type": "system"},
    {"id": "TRASH", "name": "TRASH", "type": "system"},
    {"id": "SPAM", "name": "SPAM", "type": "system"},
    {"id": "CATEGORY_PROMOTIONS", "name": "CATEGORY_PROMOTIONS", "type": "system"},
    {"id": "Label_1", "name": "Receipts", "type": "user"},
    {"id": "Label_2", "name": "Clients", "type": "user"},
]})
OK = (200, {"id": "x"})
_NOT_FOUND = (404, {"error": {"code": 404, "message": "Requested entity was not found."}})
_NO_SCOPE = (403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                             "status": "PERMISSION_DENIED"}})
# Standalone: the default google_oauth.scopes already has gmail.modify — just reconnect.
_STANDALONE_FIX = "already include it — reconnect Google (/auth/google/login) to grant it"
_FD_FIX = "A Flight Deck admin ticks it in Connections → Google"
_OTHER_LABELS_STAY = "Their other labels stay on"


# ── archive ──────────────────────────────────────────────────────────


async def test_archive_threads_removes_inbox_from_each_conversation(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("POST", "/threads/t1/modify"): OK, ("POST", "/threads/t2/modify"): OK})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1", "t2"])
    assert res.success, res.error
    assert gmail.calls() == [("/threads/t1/modify", {"removeLabelIds": ["INBOX"]}),
                             ("/threads/t2/modify", {"removeLabelIds": ["INBOX"]})]
    assert not gmail.calls("GET")  # archive needs no labels.list
    assert res.content.startswith("Archived 2 conversations — removed from the Inbox.")
    assert "Thread IDs: t1, t2" in res.content
    assert "Undo: google_mail action=move label=INBOX thread_ids=[t1, t2]" in res.content
    assert "Note: Gmail shows conversations" not in res.content


async def test_archive_messages_is_one_batch_modify(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("POST", "/messages/batchModify"): (204, None)})  # empty body
    res = await _tool_with(gmail).execute("archive", message_ids=["m1", "m2"])
    assert res.success, res.error
    assert gmail.calls() == [("/messages/batchModify",
                              {"ids": ["m1", "m2"], "removeLabelIds": ["INBOX"]})]
    assert "Archived 2 messages" in res.content and "Message IDs: m1, m2" in res.content
    # One message out of the Inbox doesn't take its conversation out.
    assert "a conversation stays in the Inbox while another of its messages is there" in res.content
    assert "pass thread_ids" in res.content


@pytest.mark.parametrize("status, why", [
    (400, "rejected: Invalid id value"),
    (404, "not found"),  # one of the IDs is gone
])
async def test_batch_failure_retries_one_by_one_to_isolate_the_bad_id(monkeypatch, status, why):
    _set_mode(monkeypatch)
    bad = (status, {"error": {"code": status, "message": "Invalid id value"}})
    gmail = _Gmail({
        ("POST", "/messages/batchModify"): bad,
        ("POST", "/messages/m1/modify"): OK,
        ("POST", "/messages/m3/modify"): OK,
        ("POST", "/messages/bad/modify"): bad,
    })
    res = await _tool_with(gmail).execute("archive", message_ids=["m1", "bad", "m3"])
    assert res.success, res.error
    assert [p for p, _ in gmail.calls()] == [
        "/messages/batchModify", "/messages/m1/modify", "/messages/bad/modify", "/messages/m3/modify"]
    assert gmail.calls()[1][1] == {"removeLabelIds": ["INBOX"]}  # no ids in a single modify
    assert "Archived 2 messages" in res.content and "Message IDs: m1, m3" in res.content
    assert f"NOT archived: bad ({why})" in res.content
    assert "message_ids=[m1, m3]" in res.content  # the undo names only what changed


async def test_a_single_bad_message_is_not_retried(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("POST", "/messages/batchModify"): _NOT_FOUND})
    res = await _tool_with(gmail).execute("archive", message_id="m9")
    assert not res.success
    assert "NOT archived: m9 (not found)" in res.error and "Nothing was changed." in res.error
    assert len(gmail.calls()) == 1


async def test_partial_thread_failure_still_reports_what_changed(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("POST", "/threads/t1/modify"): OK, ("POST", "/threads/t2/modify"): OK,
                    ("POST", "/threads/gone/modify"): _NOT_FOUND})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1", "gone", "t2"])
    assert res.success, res.error
    assert not res.content.startswith("Error")
    assert "Archived 2 conversations" in res.content and "Thread IDs: t1, t2" in res.content
    assert "NOT archived: gone (not found)" in res.content
    assert "thread_ids=[t1, t2]" in res.content and "gone]" not in res.content


async def test_all_failed_is_a_failure_with_nothing_changed(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({})  # every modify 404s
    res = await _tool_with(gmail).execute("archive", thread_ids=["a", "b"], message_ids=["c"])
    assert not res.success
    assert res.error.startswith("Nothing was archived:")
    for target in ("a", "b", "c"):
        assert f"NOT archived: {target} (not found)" in res.error
    assert "Nothing was changed. Check the IDs" in res.error


async def test_a_batch_rate_limit_is_not_retried_one_by_one(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("POST", "/messages/batchModify"): (429, {"error": {"message": "Too many"}})})
    res = await _tool_with(gmail).execute("archive", message_ids=["m1", "m2"])
    assert not res.success
    assert gmail.calls() == [("/messages/batchModify",
                              {"ids": ["m1", "m2"], "removeLabelIds": ["INBOX"]})]
    for target in ("m1", "m2"):
        assert f"NOT archived: {target} (Gmail rate limit — try again in a moment)" in res.error
    assert res.error.endswith("Nothing was changed.")  # not "Check the IDs" — they're fine


async def test_a_batch_server_error_fails_its_messages_but_not_the_threads(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("POST", "/threads/t1/modify"): OK,
                    ("POST", "/messages/batchModify"): (500, {"error": {"message": "Backend"}})})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1"], message_ids=["m1", "m2"])
    assert res.success, res.error
    assert [p for p, _ in gmail.calls()] == ["/threads/t1/modify", "/messages/batchModify"]
    assert "Archived 1 conversation" in res.content and "Message IDs" not in res.content
    for target in ("m1", "m2"):
        assert f"NOT archived: {target} (Gmail API error 500)" in res.content
    assert "Undo: google_mail action=move label=INBOX thread_ids=[t1]" in res.content
    assert "message_ids=[" not in res.content


async def test_expired_authentication_on_every_target_says_reconnect(monkeypatch):
    _set_mode(monkeypatch)
    expired = (401, {"error": {"code": 401, "message": "Invalid Credentials"}})
    gmail = _Gmail({("POST", "/threads/t1/modify"): expired,
                    ("POST", "/messages/batchModify"): expired})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1"], message_ids=["m1", "m2"])
    assert not res.success
    assert res.error == ("Google authentication expired. Please reconnect your Google account. "
                         "Nothing was changed.")


async def test_at_most_five_gmail_requests_run_at_once(monkeypatch):
    _set_mode(monkeypatch)
    state = {"now": 0, "peak": 0}

    async def gmail(request: httpx.Request) -> httpx.Response:
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)  # stays in flight while the others start
        state["now"] -= 1
        if request.url.path.endswith("/batchModify"):  # retried one by one
            return httpx.Response(400, json={"error": {"message": "Invalid id value"}})
        return httpx.Response(200, json={"id": "x"})

    tool = GoogleMailTool()
    tool._client = httpx.AsyncClient(transport=httpx.MockTransport(gmail))
    res = await tool.execute("archive", thread_ids=[f"t{i}" for i in range(30)],
                             message_ids=[f"m{i}" for i in range(30)])
    assert res.success, res.error
    assert "Archived 30 conversations and 30 messages" in res.content
    assert 1 < state["peak"] <= 5, state["peak"]


@pytest.mark.parametrize("kwargs, threads, messages", [
    ({"thread_ids": '["t1", "t2"]', "thread_id": "t1"}, ["t1", "t2"], []),
    ({"message_ids": "m1, m2 m3"}, [], ["m1", "m2", "m3"]),
    ({"thread_ids": "[t1, t2]", "message_ids": ["m1", "m1"], "message_id": "m2"}, ["t1", "t2"], ["m1", "m2"]),
    ({"thread_ids": "['t1','t2']"}, ["t1", "t2"], []),
])
async def test_ids_as_strings_json_or_lists_are_parsed_and_deduplicated(
        monkeypatch, kwargs, threads, messages):
    _set_mode(monkeypatch)
    gmail = _Gmail({("POST", "/messages/batchModify"): (204, None),
                    **{("POST", f"/threads/{t}/modify"): OK for t in threads}})
    res = await _tool_with(gmail).execute("archive", **kwargs)
    assert res.success, res.error
    sent = gmail.calls()
    assert [p for p, _ in sent if p.startswith("/threads/")] == [f"/threads/{t}/modify" for t in threads]
    batches = [body["ids"] for p, body in sent if p == "/messages/batchModify"]
    assert batches == ([messages] if messages else [])


async def test_no_targets_and_too_many_targets_are_refused_without_http(monkeypatch):
    calls = _set_mode(monkeypatch)
    gmail = _Gmail({})
    tool = _tool_with(gmail)

    res = await tool.execute("archive")
    assert not res.success and "thread_ids" in res.error and "Nothing was changed." in res.error

    res = await tool.execute("add_labels", labels=["Receipts"],
                             thread_ids=[f"t{i}" for i in range(60)],
                             message_ids=[f"m{i}" for i in range(41)])
    assert not res.success
    assert res.error == "At most 100 emails per call — call again for the rest. Nothing was changed."
    assert gmail.requests == []
    assert calls["get_tokens"] == 2  # the token, but no Gmail call

    gmail = _Gmail({("POST", "/messages/batchModify"): (204, None)})
    res = await _tool_with(gmail).execute("archive", message_ids=[f"m{i}" for i in range(100)])
    assert res.success, res.error
    assert "Archived 100 messages" in res.content and "… and 80 more" in res.content
    assert "with the same thread_ids / message_ids" in res.content  # too many to repeat


async def test_attachments_are_ignored_by_organize_actions(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1"], attachments=["/nope.pdf"],
                                          remove_attachments=["all"])
    assert res.success, res.error
    assert "attach" not in res.content.lower()


# ── move ─────────────────────────────────────────────────────────────


async def test_move_to_a_user_label_adds_it_and_leaves_the_inbox(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK,
                    ("POST", "/messages/batchModify"): (204, None)})
    res = await _tool_with(gmail).execute("move", label="receipts", thread_ids=["t1"], message_ids=["m1"])
    assert res.success, res.error
    body = {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]}
    assert gmail.calls() == [("/threads/t1/modify", body),
                             ("/messages/batchModify", {"ids": ["m1"], **body})]
    assert len(gmail.calls("GET")) == 1  # one labels.list per call
    assert "Moved 1 conversation and 1 message to 'Receipts'" in res.content
    assert "Note: Gmail shows conversations" in res.content
    assert _OTHER_LABELS_STAY in res.content
    # The undo also brings them back to the Inbox — removing the label alone wouldn't.
    assert res.content.splitlines()[-1] == (
        'Undo: google_mail action=remove_labels labels=["Receipts"] thread_ids=[t1] message_ids=[m1], '
        "then action=move label=INBOX with the same IDs")


async def test_move_to_inbox_only_adds_inbox(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label="Inbox", thread_ids=["t1"])
    assert res.success, res.error
    assert gmail.calls() == [("/threads/t1/modify", {"addLabelIds": ["INBOX"]})]
    assert "Moved 1 conversation back to the Inbox." in res.content
    assert _OTHER_LABELS_STAY in res.content  # still under Receipts, if it was
    assert "Undo: google_mail action=archive thread_ids=[t1]" in res.content


async def test_moving_between_labels_says_the_old_label_stays(monkeypatch):
    # "Move the Acme threads from Receipts to Clients": move adds Clients and
    # removes INBOX — Receipts stays on, and the answer says how to take it off.
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label="Clients", thread_ids=["t1"])
    assert res.success, res.error
    assert gmail.calls() == [("/threads/t1/modify",
                              {"addLabelIds": ["Label_2"], "removeLabelIds": ["INBOX"]})]
    assert "Moved 1 conversation to 'Clients'" in res.content
    assert (_OTHER_LABELS_STAY + " — to take one off too (e.g. the label they were filed under "
            'before), google_mail action=remove_labels labels=["<label>"] with the same IDs.') \
        in res.content


@pytest.mark.parametrize("dest, hint", [
    ("TRASH", "out of scope"), ("spam", "out of scope"), ("SENT", "out of scope"),
    ("Sent Mail", "out of scope"), ("Drafts", "out of scope"),  # Gmail's folder names
    ("UNREAD", "add_labels"), ("starred", "add_labels"), ("promotions", "add_labels"),
])
async def test_move_refuses_destinations_that_are_not_places(monkeypatch, dest, hint):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label=dest, thread_ids=["t1"])
    assert not res.success
    assert hint in res.error and "Nothing was changed." in res.error
    assert "create_label" not in res.error  # never "make a label called Sent Mail"
    assert gmail.calls() == []


@pytest.mark.parametrize("dest", ["Archive", "archived", "All Mail"])
async def test_move_to_the_archive_points_at_action_archive(monkeypatch, dest):
    # Gmail has no Archive label: "move these to the archive" is action=archive,
    # not a new label called Archive.
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label=dest, thread_ids=["t1"])
    assert not res.success
    assert res.error == (
        "Gmail has no Archive label — archiving just takes emails out of the Inbox (All Mail "
        "still shows them). Use google_mail action=archive with the same IDs instead. "
        "Nothing was changed.")
    assert gmail.calls() == []


def _with_own_label(name: str, label_id: str = "Label_9") -> tuple[int, dict]:
    """LABELS plus a user label *name* of the mailbox's own."""
    return 200, {"labels": [*LABELS[1]["labels"], {"id": label_id, "name": name, "type": "user"}]}


async def test_move_to_a_user_label_called_archive_files_there(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): _with_own_label("Archive"), ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label="archive", thread_ids=["t1"])
    assert res.success, res.error
    assert gmail.calls() == [("/threads/t1/modify",
                              {"addLabelIds": ["Label_9"], "removeLabelIds": ["INBOX"]})]

    # An exact user label wins over the archive pointer — "Archived" too.
    gmail = _Gmail({("GET", "/labels"): _with_own_label("Archived", "Label_8"),
                    ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label="ARCHIVED", thread_ids=["t1"])
    assert res.success, res.error
    assert gmail.calls() == [("/threads/t1/modify",
                              {"addLabelIds": ["Label_8"], "removeLabelIds": ["INBOX"]})]


@pytest.mark.parametrize("dest, why", [
    ("archived", "No such Gmail label: 'archived'"),
    ("All Mail", "'All Mail' is Gmail's view of every email, not a label"),
])
async def test_move_to_an_archive_name_points_at_the_mailboxs_own_archive_label(
        monkeypatch, dest, why):
    # With a label "Archive" of its own, "Gmail has no Archive label" would be
    # false — and would hide the label the user may have meant.
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): _with_own_label("Archive"), ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label=dest, thread_ids=["t1"])
    assert not res.success
    assert res.error == (
        f"{why} — this mailbox has its own label 'Archive': to file them there, google_mail "
        'action=move label="Archive" with the same IDs. Nothing was changed.')
    assert "no Archive label" not in res.error and "action=archive" not in res.error
    assert gmail.calls() == []


async def test_add_labels_with_an_archive_name_points_at_the_mailboxs_own_archive_label(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): _with_own_label("Archive"), ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("add_labels", labels=["archived"], thread_ids=["t1"])
    assert not res.success
    assert "No such Gmail label: 'archived'. Existing labels: Archive, Clients, Receipts." in res.error
    assert ("This mailbox has its own label 'Archive' — pass that name if it's the one meant."
            in res.error)
    assert "no Archive label" not in res.error and "action=archive" not in res.error
    # Not "make a label called archived" next to Archive.
    assert "create_label" not in res.error
    assert res.error.endswith("Nothing was changed.")
    assert gmail.calls() == []


@pytest.mark.parametrize("kwargs, named", [
    ({"label": "Receipts", "labels": ["Archive"]}, "'Receipts', 'Archive'"),
    ({"label": "All Mail", "labels": ["Receipts"]}, "'All Mail', 'Receipts'"),
    ({"label": "Receipts", "labels": ["Invoices 2026"]}, "'Receipts', 'Invoices 2026'"),
])
async def test_move_checks_one_destination_before_the_names_themselves(monkeypatch, kwargs, named):
    # A leftover list naming the archive must not turn "file under Receipts"
    # into "use action=archive instead" — the conflict is what's wrong.
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", thread_ids=["t1"], **kwargs)
    assert not res.success
    assert res.error == (
        f"move takes one destination, but label / labels name {named} — pass only "
        "label=<the destination> (add_labels adds more than one). Nothing was changed.")
    assert gmail.calls() == []


async def test_move_to_the_archive_named_twice_is_one_destination(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label="Archive", labels=["archived", "all mail"],
                                          thread_ids=["t1"])
    assert not res.success
    assert res.error.startswith("Gmail has no Archive label")
    assert gmail.calls() == []


async def test_move_needs_one_destination(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS})
    res = await _tool_with(gmail).execute("move", thread_ids=["t1"])
    assert not res.success and "label=<label name or ID>" in res.error
    assert gmail.requests == []


async def test_move_refuses_label_and_labels_naming_different_destinations(monkeypatch):
    # A leftover label=INBOX must not send the mail back to the Inbox instead of to Receipts.
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("move", label="INBOX", labels=["Receipts"],
                                          thread_ids=["t1"])
    assert not res.success
    assert "move takes one destination, but label / labels name 'INBOX', 'Receipts'" in res.error
    assert res.error.endswith("Nothing was changed.")
    assert gmail.calls() == []

    # The same label both ways — or a one-name labels list alone — is one destination.
    for kwargs in ({"label": "Receipts", "labels": ["receipts", "Label_1"]}, {"labels": "Receipts"}):
        gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
        res = await _tool_with(gmail).execute("move", thread_ids=["t1"], **kwargs)
        assert res.success, (kwargs, res.error)
        assert gmail.calls() == [("/threads/t1/modify",
                                  {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]})]


# ── add_labels / remove_labels ───────────────────────────────────────


async def test_add_labels_resolves_names_ids_and_system_aliases(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute(
        "add_labels", labels='["receipts", "Promotions", "starred", "Label_2", "RECEIPTS"]',
        thread_id="t1")
    assert res.success, res.error
    assert gmail.calls() == [("/threads/t1/modify", {
        "addLabelIds": ["Label_1", "CATEGORY_PROMOTIONS", "STARRED", "Label_2"]})]
    assert ("Added labels 'Receipts', 'CATEGORY_PROMOTIONS', 'STARRED', 'Clients' "
            "to 1 conversation.") in res.content
    assert 'action=remove_labels labels=["Receipts", "CATEGORY_PROMOTIONS", "STARRED", "Clients"]' \
        in res.content


async def test_a_comma_separated_label_string_works(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("add_labels", labels="Receipts, clients", thread_ids=["t1"])
    assert res.success, res.error
    assert gmail.calls()[0][1] == {"addLabelIds": ["Label_1", "Label_2"]}


async def test_an_unknown_label_refuses_the_whole_call(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("add_labels", labels=["Receipts", "Invoices 2026"],
                                          thread_ids=["t1"])
    assert not res.success
    assert "No such Gmail label: 'Invoices 2026'" in res.error
    assert "Existing labels: Clients, Receipts." in res.error  # user labels, not system ones
    assert 'action=create_label label="Invoices 2026"' in res.error
    assert "Nothing was changed." in res.error
    assert gmail.calls() == []  # no modify, nothing created


async def test_an_unknown_archive_label_points_at_archive_not_only_create_label(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("add_labels", labels=["All Mail"], thread_ids=["t1"])
    assert not res.success
    assert "No such Gmail label: 'All Mail'" in res.error
    assert ("Gmail has no Archive label — archived mail is just mail out of the Inbox (All Mail "
            "still shows it): google_mail action=archive takes emails out, action=move "
            "label=INBOX brings them back.") in res.error
    # create_label refuses "All Mail" — so the refusal doesn't send the model there.
    assert "create_label" not in res.error
    assert res.error.endswith("Nothing was changed.")

    # "Archive" is a name a label may take: create_label stays on offer, for a user who asked.
    res = await _tool_with(gmail).execute("add_labels", labels=["Archive"], thread_ids=["t1"])
    assert not res.success
    assert "action=archive takes emails out" in res.error
    assert 'action=create_label label="Archive" first' in res.error
    assert gmail.calls() == []


@pytest.mark.parametrize("action, removed", [("add_labels", None), ("remove_labels", ["UNREAD"])])
async def test_a_lone_label_param_does_not_join_the_labels_list(monkeypatch, action, removed):
    # After list_messages label=INBOX the model may pass label="INBOX" along:
    # marking read must not archive too, and tagging must not un-archive.
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    labels = ["UNREAD"] if removed else ["Receipts"]
    res = await _tool_with(gmail).execute(action, labels=labels, label="INBOX", thread_ids=["t1"])
    assert res.success, res.error
    body = {"removeLabelIds": ["UNREAD"]} if removed else {"addLabelIds": ["Label_1"]}
    assert gmail.calls() == [("/threads/t1/modify", body)]
    assert "INBOX" not in res.content

    # Without labels, a lone label is the one to change.
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute(action, label="Receipts", thread_ids=["t1"])
    assert res.success, res.error
    key = "removeLabelIds" if removed else "addLabelIds"
    assert gmail.calls() == [("/threads/t1/modify", {key: ["Label_1"]})]


@pytest.mark.parametrize("label", ["TRASH", "spam", "SENT", "DRAFT", "CHAT", "Sent Mail", "drafts"])
async def test_trash_spam_and_sent_labels_are_never_touched(monkeypatch, label):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    for action in ("add_labels", "remove_labels"):
        res = await _tool_with(gmail).execute(action, labels=[label], thread_ids=["t1"])
        assert not res.success and "out of scope" in res.error, (action, res.error)
    assert gmail.calls() == []


async def test_remove_unread_marks_read(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/messages/batchModify"): (204, None)})
    res = await _tool_with(gmail).execute("remove_labels", labels=["unread"], message_ids=["m1"])
    assert res.success, res.error
    assert gmail.calls() == [("/messages/batchModify", {"ids": ["m1"], "removeLabelIds": ["UNREAD"]})]
    assert "Removed label 'UNREAD' from 1 message (marked read)." in res.content
    assert 'Undo: google_mail action=add_labels labels=["UNREAD"] message_ids=[m1]' in res.content
    assert "Note: Gmail shows conversations" not in res.content  # INBOX untouched


# ── create_label ─────────────────────────────────────────────────────


def _create_routes(existing: list[dict], made: list[str]) -> dict:
    def create(request: httpx.Request):
        name = json.loads(request.content)["name"]
        made.append(name)
        return 200, {"id": f"Label_{len(made) + 10}", "name": name, "type": "user"}

    return {("GET", "/labels"): (200, {"labels": existing}), ("POST", "/labels"): create}


async def test_create_label_posts_a_visible_label(monkeypatch):
    _set_mode(monkeypatch)
    made: list[str] = []
    gmail = _Gmail(_create_routes(LABELS[1]["labels"], made))
    res = await _tool_with(gmail).execute("create_label", label="  Invoices ")
    assert res.success, res.error
    assert gmail.calls() == [("/labels", {"name": "Invoices", "labelListVisibility": "labelShow",
                                          "messageListVisibility": "show"})]
    assert res.content.startswith("Created label 'Invoices'.")
    assert "Label ID: Label_11" in res.content
    assert 'action=add_labels labels=["Invoices"]' in res.content


async def test_create_label_that_exists_creates_nothing(monkeypatch):
    _set_mode(monkeypatch)
    made: list[str] = []
    gmail = _Gmail(_create_routes(LABELS[1]["labels"], made))
    res = await _tool_with(gmail).execute("create_label", label="receipts")
    assert res.success, res.error
    assert res.content == "Label 'Receipts' already exists (ID: Label_1) — nothing created."
    assert made == []


async def test_create_nested_label_creates_the_missing_parent_first(monkeypatch):
    _set_mode(monkeypatch)
    made: list[str] = []
    gmail = _Gmail(_create_routes(LABELS[1]["labels"], made))
    res = await _tool_with(gmail).execute("create_label", label="Projects / Acme")
    assert res.success, res.error
    assert made == ["Projects", "Projects/Acme"]
    assert "Created label 'Projects/Acme'." in res.content and "Label ID: Label_12" in res.content
    assert "Also created its parent label: 'Projects' (Label ID: Label_11)" in res.content

    # An existing parent keeps its own spelling; only the child is created.
    made.clear()
    res = await _tool_with(gmail).execute("create_label", label="clients/Acme")
    assert res.success, res.error
    assert made == ["Clients/Acme"]
    assert "parent" not in res.content


@pytest.mark.parametrize("name", ["Inbox", "inbox/Receipts"])
async def test_create_label_refuses_system_names_without_http(monkeypatch, name):
    _set_mode(monkeypatch)
    gmail = _Gmail({})
    res = await _tool_with(gmail).execute("create_label", label=name)
    assert not res.success
    # move label=INBOX works (back to the Inbox), so both are on offer.
    assert "is a Gmail system label — use it with add_labels / move" in res.error
    assert "Nothing was created." in res.error
    assert gmail.requests == []


@pytest.mark.parametrize("name", [
    "STARRED", "Starred", "important", "Unread", "category_social", "Starred/Old", "Promotions",
])
async def test_create_label_points_marker_labels_at_add_labels_only(monkeypatch, name):
    # move refuses these ("isn't a place to move email to … use add_labels") —
    # so create_label must not offer move either.
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS, ("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("create_label", label=name)
    assert not res.success
    assert "is a Gmail system label — use it with add_labels" in res.error
    assert "move" not in res.error and res.error.endswith("Nothing was created.")

    res = await _tool_with(gmail).execute("move", label=name.split("/")[0], thread_ids=["t1"])
    assert not res.success
    assert "isn't a place to move email to" in res.error and "action=add_labels" in res.error
    assert gmail.calls() == []  # nothing created, nothing moved


@pytest.mark.parametrize("name, why", [
    ("Sent Mail", "out of scope"), ("drafts", "out of scope"), ("Trash", "out of scope"),
    ("Spam/Old", "out of scope"), ("All Mail", "action=archive takes emails out of the Inbox"),
])
async def test_create_label_refuses_gmail_folder_names_without_sending_to_add_labels(
        monkeypatch, name, why):
    # add_labels / move refuse these too — the refusal mustn't point there.
    _set_mode(monkeypatch)
    gmail = _Gmail({})
    res = await _tool_with(gmail).execute("create_label", label=name)
    assert not res.success
    assert why in res.error and res.error.endswith("Nothing was created.")
    assert "add_labels" not in res.error and "move" not in res.error
    assert gmail.requests == []


@pytest.mark.parametrize("name", ["promotions", "Personal/Taxes"])
async def test_create_label_refuses_a_category_name_no_user_label_has(monkeypatch, name):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): LABELS})
    res = await _tool_with(gmail).execute("create_label", label=name)
    assert not res.success
    assert "is a Gmail system label — use it with add_labels" in res.error
    assert "move" not in res.error  # a category only marks email
    assert res.error.endswith("Nothing was created.")
    assert gmail.calls() == []  # looked, created nothing


async def test_create_label_uses_a_user_label_with_a_category_name(monkeypatch):
    # Older mailboxes have a user label "Personal"; add_labels already resolves
    # "personal" to it, so create_label must agree.
    _set_mode(monkeypatch)
    made: list[str] = []
    existing = [*LABELS[1]["labels"], {"id": "Label_3", "name": "Personal", "type": "user"}]
    gmail = _Gmail({**_create_routes(existing, made), ("POST", "/threads/t1/modify"): OK})

    res = await _tool_with(gmail).execute("create_label", label="personal")
    assert res.success, res.error
    assert res.content == "Label 'Personal' already exists (ID: Label_3) — nothing created."

    res = await _tool_with(gmail).execute("create_label", label="Personal/Taxes")
    assert res.success, res.error
    assert made == ["Personal/Taxes"]
    assert "Created label 'Personal/Taxes'." in res.content and "parent" not in res.content

    res = await _tool_with(gmail).execute("add_labels", labels=["personal"], thread_ids=["t1"])
    assert res.success, res.error
    assert gmail.calls()[-1] == ("/threads/t1/modify", {"addLabelIds": ["Label_3"]})


@pytest.mark.parametrize("name", ["", "  ", "a//b", "/a", "a/"])
async def test_create_label_refuses_empty_parts(monkeypatch, name):
    _set_mode(monkeypatch)
    gmail = _Gmail({})
    res = await _tool_with(gmail).execute("create_label", label=name)
    assert not res.success and "no empty parts" in res.error
    assert gmail.requests == []


async def test_create_label_409_reports_the_existing_label(monkeypatch):
    _set_mode(monkeypatch)
    listings = iter([[], [{"id": "Label_7", "name": "Invoices", "type": "user"}]])
    gmail = _Gmail({
        ("GET", "/labels"): lambda r: (200, {"labels": next(listings)}),
        ("POST", "/labels"): (409, {"error": {"code": 409, "message": "Label name exists or conflicts"}}),
    })
    res = await _tool_with(gmail).execute("create_label", label="invoices")
    assert res.success, res.error
    assert res.content == "Label 'Invoices' already exists (ID: Label_7) — nothing created."
    assert len(gmail.calls("GET")) == 2  # re-listed after the 409


async def test_create_label_reports_a_name_gmail_refuses(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/labels"): (200, {"labels": []}),
                    ("POST", "/labels"): (400, {"error": {"message": "Invalid label name"}})})
    res = await _tool_with(gmail).execute("create_label", label="Muted")
    assert not res.success
    assert "Gmail refused the label name 'Muted' (Invalid label name)" in res.error
    assert res.error.endswith("Nothing was created.")


async def test_create_label_failing_after_its_parent_names_what_was_created(monkeypatch):
    _set_mode(monkeypatch)
    made: list[str] = []

    def create(request: httpx.Request):
        name = json.loads(request.content)["name"]
        made.append(name)
        if "/" in name:
            return 400, {"error": {"message": "Invalid label name"}}
        return 200, {"id": "Label_11", "name": name, "type": "user"}

    gmail = _Gmail({("GET", "/labels"): (200, {"labels": []}), ("POST", "/labels"): create})
    res = await _tool_with(gmail).execute("create_label", label="Projects/Acme")
    assert not res.success
    assert made == ["Projects", "Projects/Acme"]
    assert "Gmail refused the label name 'Projects/Acme' (Invalid label name)" in res.error
    assert res.error.endswith("Created before that: 'Projects' (Label ID: Label_11).")
    assert "Nothing was created" not in res.error  # the parent was


async def test_create_label_409_on_the_parent_reuses_it(monkeypatch):
    _set_mode(monkeypatch)
    made: list[str] = []
    listings = iter([[], [{"id": "Label_7", "name": "Projects", "type": "user"}]])

    def create(request: httpx.Request):
        name = json.loads(request.content)["name"]
        made.append(name)
        if name == "projects":  # made meanwhile, by someone else
            return 409, {"error": {"code": 409, "message": "Label name exists or conflicts"}}
        return 200, {"id": "Label_12", "name": name, "type": "user"}

    gmail = _Gmail({("GET", "/labels"): lambda r: (200, {"labels": next(listings)}),
                    ("POST", "/labels"): create})
    res = await _tool_with(gmail).execute("create_label", label="projects/Acme")
    assert res.success, res.error
    assert made == ["projects", "Projects/Acme"]  # nested under the existing spelling
    assert len(gmail.calls("GET")) == 2  # re-listed after the 409
    assert res.content.startswith("Created label 'Projects/Acme'.\n  Label ID: Label_12")
    assert "parent" not in res.content  # it wasn't created by this call


# ── the gmail.modify scope ───────────────────────────────────────────


@pytest.mark.parametrize("fd, fix", [(False, _STANDALONE_FIX), (True, _FD_FIX)])
@pytest.mark.parametrize("action, args", [
    ("archive", {"thread_ids": ["t1"]}),
    ("move", {"thread_ids": ["t1"], "label": "Receipts"}),
    ("add_labels", {"thread_ids": ["t1"], "labels": ["Receipts"]}),
    ("remove_labels", {"message_ids": ["m1"], "labels": ["UNREAD"]}),
    ("create_label", {"label": "Invoices"}),
])
async def test_missing_modify_scope_is_refused_before_any_gmail_call(
        monkeypatch, fd, fix, action, args):
    _set_mode(monkeypatch, fd=fd, scope=f"{READ} {COMPOSE}")
    gmail = _Gmail({("GET", "/labels"): LABELS})
    res = await _tool_with(gmail).execute(action, **args)
    assert not res.success
    assert res.error.startswith(
        "Gmail organizing (archive / labels / move) needs the 'Gmail (read + modify)' scope "
        "(gmail.modify), which this Google connection doesn't include.")
    assert fix in res.error and res.error.endswith("Nothing was changed.")
    assert gmail.requests == []


async def test_full_mail_scope_also_organizes(monkeypatch):
    _set_mode(monkeypatch, scope="https://mail.google.com/")
    gmail = _Gmail({("POST", "/threads/t1/modify"): OK})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1"])
    assert res.success, res.error


@pytest.mark.parametrize("fd, fix", [(False, _STANDALONE_FIX), (True, _FD_FIX)])
async def test_gmail_403_insufficient_scope_maps_to_the_scope_message(monkeypatch, fd, fix):
    _set_mode(monkeypatch, fd=fd)  # the token claims gmail.modify; Gmail disagrees
    gmail = _Gmail({("POST", "/threads/t1/modify"): _NO_SCOPE})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1"])
    assert not res.success and res.error == gm._modify_scope_missing(fd)
    assert fix in res.error

    gmail = _Gmail({("GET", "/labels"): _NO_SCOPE})
    res = await _tool_with(gmail).execute("add_labels", labels=["x"], thread_ids=["t1"])
    assert not res.success and res.error == gm._modify_scope_missing(fd)

    gmail = _Gmail({("GET", "/labels"): (200, {"labels": []}), ("POST", "/labels"): _NO_SCOPE})
    res = await _tool_with(gmail).execute("create_label", label="Invoices")
    assert not res.success and fix in res.error
    assert res.error.endswith("Nothing was created.")

    # Any other 403 stays a plain permission error.
    gmail = _Gmail({("POST", "/threads/t1/modify"): (403, {"error": {"message": "Delegation denied"}})})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1"])
    assert not res.success and "permission denied: Delegation denied" in res.error


@pytest.mark.parametrize("scopes, add", [
    (None, False),  # the default list already has gmail.modify: reconnecting grants it
    ([READ, COMPOSE, "https://mail.google.com/", "openid"], False),
    ([_G + "drive", _G + "calendar", READ, COMPOSE, "openid", "email"], True),
])
async def test_standalone_scope_fix_depends_on_the_configured_scopes(monkeypatch, _config, scopes, add):
    if scopes is not None:
        _config.google_oauth.scopes = scopes
    _set_mode(monkeypatch, scope=f"{READ} {COMPOSE}")  # connected before gmail.modify was asked for
    gmail = _Gmail({})
    res = await _tool_with(gmail).execute("archive", thread_ids=["t1"])
    assert not res.success and gmail.requests == []
    if add:
        # A scopes list in config.yaml replaces the default whole — "add it" keeps the rest.
        assert ("Add https://www.googleapis.com/auth/gmail.modify to the google_oauth.scopes list "
                "in config.yaml (keep its other entries) and reconnect Google "
                "(/auth/google/login). Nothing was changed.") in res.error
    else:
        assert res.error.endswith(
            "The configured google_oauth.scopes already include it — reconnect Google "
            "(/auth/google/login) to grant it. Nothing was changed.")
        assert "Add " not in res.error


# ── schema, description, follow-up hint ──────────────────────────────


def test_schema_and_description_carry_the_actions_and_the_rule():
    params = GoogleMailTool.parameters["properties"]
    for action in ("archive", "move", "add_labels", "remove_labels", "create_label"):
        assert action in params["action"]["enum"]
        assert gm._ACTION_SCOPES[action] == "modify"
    for name in ("labels", "message_ids", "thread_ids"):
        assert params[name]["type"] == "array" and params[name]["items"] == {"type": "string"}
    d = GoogleMailTool.description
    assert "ORGANIZE ONLY WHEN ASKED" in d
    assert "Reading, summarizing or triaging mail never implies it" in d
    assert "gmail.modify" in d and "At most 100 emails per call" in d
    # Organizing is not an email write.
    for action in ("archive", "move", "add_labels", "remove_labels", "create_label"):
        assert not ma.is_mail_write("google_mail", action)
        assert ma.is_mailbox_change("google_mail", action)


_THREAD = {"id": "t1", "messages": [{
    "id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "internalDate": "1",
    "payload": {"headers": [{"name": "From", "value": "Ana <ana@x.co>"},
                            {"name": "Subject", "value": "Hi"}]},
}]}


async def test_follow_up_hint_offers_organizing_only_when_allowed(monkeypatch):
    _set_mode(monkeypatch)
    gmail = _Gmail({("GET", "/threads"): (200, {"threads": [{"id": "t1"}]}),
                    ("GET", "/threads/t1"): (200, _THREAD)})
    bullet = "Archive / label / move — ONLY if the user asked: google_mail action=archive"

    res = await _tool_with(gmail).execute("list_messages")
    assert res.success, res.error
    assert bullet in res.content

    with ma.bound(ma.automated("autonomy_tool", "", "deny")):
        res = await _tool_with(gmail).execute("search", query="from:ana")
    assert res.success, res.error
    assert "Thread: t1" in res.content and "Next steps" in res.content
    assert bullet not in res.content
