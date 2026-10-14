"""microsoft_mail / microsoft_calendar / microsoft_onedrive and the agent side
of the Microsoft connection.

* MicrosoftOAuthManager: Flight Deck only, the owner's token, a member's turn
  refused before any request, FD's refusal reason kept;
* the registry hides ``requires_microsoft`` tools until connected;
* mail writes go through mail_authority like google_mail's (automated turns
  refused before any token fetch), drafts never send, repeats are refused;
* organizing (archive / move / create_folder / categories) has its own
  mailbox-change guard, resolves folders by name / path, prints a moved
  message's new ID and never moves to Deleted Items / Junk / Sent / Drafts;
* calendar changes that email other people need notify_attendees=true and a
  human (or approved) turn;
* OneDrive never overwrites on upload/create and refuses text over a binary.

Graph and Flight Deck answer through httpx.MockTransport.
"""

from __future__ import annotations

import json
import time
import types

import httpx
import pytest

from captain_claw import mail_authority as ma
from captain_claw.gmail_compose import REPEAT_MARK, is_repeat_refusal

_RealAsyncClient = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _config(monkeypatch, tmp_path):
    import captain_claw.config as config_mod
    from captain_claw.config import Config
    from captain_claw.flight_deck import agent_secret

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    agent_secret.reset_cache_for_tests()
    cfg = Config()
    monkeypatch.setattr(config_mod, "_config", cfg)
    import captain_claw.microsoft_oauth_manager as mm
    mm._TOKENS = None
    mm._CONNECTED = (False, 0.0)
    yield cfg
    mm._TOKENS = None
    mm._CONNECTED = (False, 0.0)
    agent_secret.reset_cache_for_tests()


class _Graph:
    """A tiny Graph: routes keyed by (METHOD, path); records requests."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("/v1.0", 1)[-1]
        hit = self.routes.get((request.method, path))
        if callable(hit):
            hit = hit(request)
        status, body = hit if hit else (404, {"error": {"code": "itemNotFound", "message": "nope"}})
        if isinstance(body, (bytes, str)):
            return httpx.Response(status, content=body if isinstance(body, bytes) else body.encode())
        return httpx.Response(status, json=body)

    def bodies(self, method: str):
        return [json.loads(r.content or b"{}") for r in self.requests if r.method == method]


def _tool(cls, graph: _Graph, monkeypatch, scopes=("mail.readwrite", "calendars.readwrite", "files.readwrite.all")):
    import importlib

    tool = cls()
    tool._client = _RealAsyncClient(transport=httpx.MockTransport(graph))
    mod = importlib.import_module(cls.__module__)
    calls = {"token": 0}

    async def token(need, what):
        calls["token"] += 1
        return "T", frozenset(scopes)

    monkeypatch.setattr(mod, "graph_token", token)
    tool._calls = calls
    return tool


# ── token manager ────────────────────────────────────────────────────


class TestManager:
    def _fd(self, monkeypatch, handler):
        import captain_claw.microsoft_oauth_manager as mm
        from captain_claw.google_oauth_manager import GoogleOAuthManager

        monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: "http://fd.test"))
        seen = []

        def wrapped(request):
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(mm.httpx, "AsyncClient",
                            lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(wrapped), **kw))
        return seen

    async def test_standalone_has_no_microsoft(self, monkeypatch):
        from captain_claw.google_oauth_manager import GoogleOAuthManager
        from captain_claw.microsoft_oauth_manager import (
            NO_FLIGHT_DECK, MicrosoftOAuthManager, is_microsoft_connected_cached,
        )

        monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: ""))
        assert await MicrosoftOAuthManager().is_connected() is False
        assert is_microsoft_connected_cached() is False
        with pytest.raises(RuntimeError, match="need Flight Deck"):
            await MicrosoftOAuthManager().access_token()
        assert "Flight Deck" in NO_FLIGHT_DECK

    async def test_owner_token_from_flight_deck_and_cached(self, monkeypatch):
        from captain_claw.microsoft_oauth_manager import MicrosoftOAuthManager, is_microsoft_connected_cached

        seen = self._fd(monkeypatch, lambda r: httpx.Response(200, json={
            "access_token": "MS", "expires_at": time.time() + 3600, "scope": "Mail.Read"}))
        mgr = MicrosoftOAuthManager()
        assert (await mgr.access_token()).access_token == "MS"
        assert await mgr.is_connected() is True and is_microsoft_connected_cached() is True
        assert len(seen) == 1  # second call served from the cache
        assert seen[0].url.path == "/fd/microsoft/access_token"
        assert "X-FD-Speaker-Grant" not in seen[0].headers

    async def test_not_connected_is_404(self, monkeypatch):
        from captain_claw.microsoft_oauth_manager import NOT_CONNECTED, MicrosoftOAuthManager

        self._fd(monkeypatch, lambda r: httpx.Response(404, json={"detail": "No stored tokens"}))
        with pytest.raises(RuntimeError) as exc:
            await MicrosoftOAuthManager().access_token()
        assert str(exc.value) == NOT_CONNECTED

    async def test_refusal_keeps_flight_decks_reason(self, monkeypatch):
        from captain_claw.microsoft_oauth_manager import MicrosoftOAuthManager, MicrosoftRefused

        self._fd(monkeypatch, lambda r: httpx.Response(403, json={"detail": "Unknown agent"}))
        with pytest.raises(MicrosoftRefused, match="Unknown agent"):
            await MicrosoftOAuthManager().get_tokens()
        assert await MicrosoftOAuthManager().is_connected() is False

    async def test_member_turn_is_refused_without_a_request(self, monkeypatch):
        from captain_claw.google_oauth_manager import GoogleOAuthManager
        from captain_claw.microsoft_oauth_manager import MicrosoftOAuthManager, MicrosoftRefused

        seen = self._fd(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "OWNER"}))
        monkeypatch.setattr(GoogleOAuthManager, "_member_call", staticmethod(lambda: True))
        with pytest.raises(MicrosoftRefused, match="shared-agent member"):
            await MicrosoftOAuthManager().get_tokens()
        assert await MicrosoftOAuthManager().is_connected() is False
        assert seen == []


class TestRegistryGate:
    def _registry(self):
        from captain_claw.tools.microsoft_mail import MicrosoftMailTool
        from captain_claw.tools.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(MicrosoftMailTool(), metadata={"requires_microsoft": True})
        return reg

    def test_hidden_until_connected(self):
        import captain_claw.microsoft_oauth_manager as mm

        reg = self._registry()
        assert "microsoft_mail" not in [t.name for t in reg._resolve_tools()]
        mm._mark(True)
        assert "microsoft_mail" in [t.name for t in reg._resolve_tools()]
        mm._CONNECTED = (True, time.time() - 3600)  # stale → hidden again
        assert "microsoft_mail" not in [t.name for t in reg._resolve_tools()]

    def test_members_never_get_microsoft_tools(self):
        from captain_claw import speaker

        assert not {"microsoft_mail", "microsoft_calendar", "microsoft_onedrive"} & speaker.SPEAKER_TOOL_ALLOWLIST_MAX


# ── mail authority hooks ─────────────────────────────────────────────


class TestMailAuthority:
    def test_microsoft_drafts_are_mail_writes(self):
        assert ma.is_mail_write("microsoft_mail", "create_draft")
        assert ma.is_mail_write("microsoft_mail", "update_draft")
        assert not ma.is_mail_write("microsoft_mail", "read_message")
        with ma.bound(ma.automated("cron", "summarize my inbox")):
            assert ma.check_mail_write("microsoft_mail", "create_draft").startswith("[not-authorized: mail-write]")
        with ma.bound(ma.human("draft a reply to ana")):
            assert ma.check_mail_write("microsoft_mail", "create_draft") is None

    def test_action_catalog_and_flows_know_microsoft_mail(self):
        from captain_claw.flight_deck.action_catalog import is_mail_write

        assert is_mail_write({"tool": "microsoft_mail", "base_args": {"action": "create_draft"}})
        assert is_mail_write({"tool": "microsoft_mail", "base_args": {}})
        assert not is_mail_write({"tool": "microsoft_mail", "base_args": {"action": "search"}})

    def test_notify_others_needs_a_human_or_approval(self):
        with ma.bound(ma.human("invite ana")):
            assert ma.check_notify_others("invitations") is None
        with ma.bound(ma.automated("flow_tool", "", mail_write="allow")):
            assert ma.check_notify_others("invitations") is None
        with ma.bound(ma.automated("cron", "email ana@x.co the agenda and invite her")):
            assert ma.is_refusal(ma.check_notify_others("invitations to ana@x.co"))


# ── microsoft_mail ───────────────────────────────────────────────────

_ME = ("GET", "/me")
_ME_OK = (200, {"mail": "me@contoso.com", "userPrincipalName": "me@contoso.com"})
_EMPTY = (200, {"value": []})


def _mail(graph, monkeypatch, **kw):
    from captain_claw.tools.microsoft_mail import MicrosoftMailTool

    return _tool(MicrosoftMailTool, graph, monkeypatch, **kw)


class TestMicrosoftMail:
    async def test_automated_turn_refused_before_token_or_http(self, monkeypatch):
        graph = _Graph({})
        tool = _mail(graph, monkeypatch)
        with ma.bound(ma.automated("cron", "summarize my inbox")):
            res = await tool.execute("create_draft", to="bob@x.co", subject="s", body="b")
        assert not res.success and res.error.startswith("[not-authorized: mail-write]")
        assert graph.requests == [] and tool._calls["token"] == 0

    @pytest.mark.parametrize("action", ["send", "send_draft"])
    async def test_never_sends(self, monkeypatch, action):
        graph = _Graph({})
        res = await _mail(graph, monkeypatch).execute(action, draft_id="d", to="a@b.co")
        assert not res.success and "doesn't send" in res.error and graph.requests == []

    async def test_create_draft_posts_a_message(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders/drafts/messages"): _EMPTY,
            ("GET", "/me/mailFolders/sentitems/messages"): _EMPTY,
            ("POST", "/me/messages"): (201, {"id": "D1", "subject": "Hi",
                                             "toRecipients": [{"emailAddress": {"address": "ana@x.co"}}]}),
        })
        res = await _mail(graph, monkeypatch).execute(
            "create_draft", to="Ana <ana@x.co>", subject="Hi", body="Hello")
        assert res.success, res.error
        assert "Draft ID: D1" in res.content and "not sent" in res.content
        body = graph.bodies("POST")[0]
        assert body["toRecipients"] == [{"emailAddress": {"address": "ana@x.co", "name": "Ana"}}]
        assert body["body"] == {"contentType": "text", "content": "Hello"}

    async def test_repeat_of_a_draft_is_refused(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders/drafts/messages"): (200, {"value": [{
                "id": "OLD", "subject": "RE: Hi", "isDraft": True,
                "toRecipients": [{"emailAddress": {"address": "ana@x.co"}}]}]}),
            ("GET", "/me/mailFolders/sentitems/messages"): _EMPTY,
        })
        res = await _mail(graph, monkeypatch).execute("create_draft", to="ana@x.co", subject="Hi", body="x")
        assert not res.success and REPEAT_MARK in res.error and is_repeat_refusal(res.error)
        assert "Draft ID OLD" in res.error and graph.bodies("POST") == []

    async def test_allow_repeat_skips_the_check(self, monkeypatch):
        graph = _Graph({("POST", "/me/messages"): (201, {"id": "D2"})})
        res = await _mail(graph, monkeypatch).execute(
            "create_draft", to="ana@x.co", subject="Hi", body="x", allow_repeat=True)
        assert res.success and all(r.method == "POST" for r in graph.requests)

    async def test_reply_draft_uses_create_reply_in_the_conversation(self, monkeypatch):
        graph = _Graph({
            _ME: _ME_OK,
            ("GET", "/me/messages/M1"): (200, {
                "id": "M1", "subject": "Plan", "conversationId": "C1",
                "from": {"emailAddress": {"address": "ana@x.co", "name": "Ana"}},
                "toRecipients": [{"emailAddress": {"address": "me@contoso.com"}}],
                "receivedDateTime": "2026-10-01T08:00:00Z"}),
            ("GET", "/me/messages"): _EMPTY,  # nothing else in the conversation
            ("POST", "/me/messages/M1/createReply"): (201, {"id": "R1", "subject": "RE: Plan"}),
        })
        res = await _mail(graph, monkeypatch).execute(
            "create_draft", reply_to_message_id="M1", body="Sounds good")
        assert res.success, res.error
        reply = graph.bodies("POST")[0]
        assert reply["comment"] == "Sounds good"
        assert reply["message"]["toRecipients"][0]["emailAddress"]["address"] == "ana@x.co"

    async def test_reply_already_drafted_in_the_conversation_is_a_repeat(self, monkeypatch):
        graph = _Graph({
            _ME: _ME_OK,
            ("GET", "/me/messages/M1"): (200, {
                "id": "M1", "subject": "Plan", "conversationId": "C1",
                "from": {"emailAddress": {"address": "ana@x.co"}},
                "receivedDateTime": "2026-10-01T08:00:00Z"}),
            ("GET", "/me/messages"): (200, {"value": [{"id": "R0", "isDraft": True, "subject": "RE: Plan"}]}),
        })
        res = await _mail(graph, monkeypatch).execute("create_draft", reply_to_message_id="M1", body="x")
        assert not res.success and is_repeat_refusal(res.error) and graph.bodies("POST") == []

    async def test_update_draft_refuses_a_sent_message(self, monkeypatch):
        graph = _Graph({("GET", "/me/messages/S1"): (200, {"id": "S1", "isDraft": False})})
        res = await _mail(graph, monkeypatch).execute("update_draft", draft_id="S1", body="new")
        assert not res.success and "not a draft" in res.error

    async def test_list_groups_by_conversation(self, monkeypatch):
        msgs = [
            {"id": "m3", "conversationId": "c1", "subject": "Plan", "isRead": False,
             "from": {"emailAddress": {"name": "Ana", "address": "ana@x.co"}},
             "receivedDateTime": "2026-10-03T08:00:00Z"},
            {"id": "m2", "conversationId": "c2", "subject": "Lunch", "isRead": True,
             "from": {"emailAddress": {"address": "bo@x.co"}}, "receivedDateTime": "2026-10-02T08:00:00Z"},
            {"id": "m1", "conversationId": "c1", "subject": "Plan", "isRead": False,
             "from": {"emailAddress": {"name": "Me", "address": "me@contoso.com"}},
             "receivedDateTime": "2026-10-01T08:00:00Z"},
        ]
        graph = _Graph({("GET", "/me/mailFolders/inbox/messages"): (200, {"value": msgs})})
        res = await _mail(graph, monkeypatch).execute("list_messages")
        assert "2 conversations, 1 with unread mail" in res.content
        assert "Newest msg: m3" in res.content and "Conversation: c1" in res.content
        assert "microsoft_mail action=read_message" in res.content

    async def test_get_attachment_saves_under_saved_downloads(self, monkeypatch, tmp_path):
        graph = _Graph({
            ("GET", "/me/messages/M1/attachments"): (200, {"value": [{
                "@odata.type": "#microsoft.graph.fileAttachment", "id": "A1",
                "name": "invoice.pdf", "contentType": "application/pdf", "size": 5}]}),
            ("GET", "/me/messages/M1/attachments/A1/$value"): (200, b"%PDF-"),
        })
        runtime = {"_saved_base_path": str(tmp_path / "saved"), "_session_id": "s1"}
        res = await _mail(graph, monkeypatch).execute("get_attachment", message_id="M1", **runtime)
        assert res.success, res.error
        assert "Saved to:" in res.content and "pdf_extract" in res.content
        saved = list((tmp_path / "saved" / "downloads" / "s1").rglob("invoice.pdf"))
        assert saved and saved[0].read_bytes() == b"%PDF-"


# ── microsoft_mail: organizing ───────────────────────────────────────

_ORGANIZE = ["archive", "move", "create_folder", "add_categories", "remove_categories"]
_ARCHIVE = ("GET", "/me/mailFolders/archive")
_ARCHIVE_OK = (200, {"id": "ARCH", "displayName": "Archive"})


def _by_name(folders: dict):
    """A folder list answering ``$filter=displayName eq '…'`` from *folders*
    (name → ID); without a filter, every folder."""
    def answer(request):
        flt = request.url.params.get("$filter", "")
        if not flt:
            return 200, {"value": [{"id": i, "displayName": n} for n, i in folders.items()]}
        name = flt.split("'", 1)[1].rsplit("'", 1)[0].replace("''", "'")
        hit = folders.get(name)
        return 200, {"value": [{"id": hit, "displayName": name}] if hit else []}
    return answer


def _posts(graph):
    return [r for r in graph.requests if r.method == "POST"]


class TestMicrosoftMailOrganize:
    def test_organizing_is_a_mailbox_change_not_an_email_write(self):
        from captain_claw.tools.microsoft_mail import MicrosoftMailTool

        for action in _ORGANIZE:
            assert ma.is_mailbox_change("microsoft_mail", action)
            assert not ma.is_mail_write("microsoft_mail", action)
        assert set(_ORGANIZE) <= set(MicrosoftMailTool.parameters["properties"]["action"]["enum"])
        assert {"categories", "message_ids", "thread_ids"} <= set(MicrosoftMailTool.parameters["properties"])
        d = MicrosoftMailTool.description
        assert "WRITE ONLY WHEN ASKED" in d and "ORGANIZE ONLY WHEN ASKED" in d
        assert "Moving an Outlook message changes its Message ID" in d

    @pytest.mark.parametrize("action", _ORGANIZE)
    async def test_automated_turn_refused_before_token_or_http(self, monkeypatch, action):
        graph = _Graph({_ARCHIVE: _ARCHIVE_OK})
        tool = _mail(graph, monkeypatch)
        with ma.bound(ma.automated("cron", "summarize my inbox")):
            res = await tool.execute(action, message_ids=["M1"], folder="Receipts", categories=["X"])
        assert not res.success and res.error.startswith("[not-authorized: mail-write]")
        assert graph.requests == [] and tool._calls["token"] == 0

    async def test_a_job_that_asks_to_archive_proceeds(self, monkeypatch):
        graph = _Graph({_ARCHIVE: _ARCHIVE_OK, ("POST", "/me/messages/M1/move"): (201, {"id": "N1"})})
        with ma.bound(ma.automated("cron", "archive newsletters")):
            res = await _mail(graph, monkeypatch).execute("archive", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "ARCH"}]

    async def test_more_than_100_emails_refused_before_token_or_http(self, monkeypatch):
        graph = _Graph({})
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("archive", thread_ids=[f"C{i}" for i in range(60)],
                                 message_ids=",".join(f"M{i}" for i in range(41)))
        assert not res.success
        assert res.error == "At most 100 emails per call — call again for the rest. Nothing was changed."
        res = await tool.execute("add_categories", categories=["X"])
        assert not res.success and "Which emails?" in res.error
        assert graph.requests == [] and tool._calls["token"] == 0

    async def test_organizing_needs_mail_readwrite(self, monkeypatch):
        import captain_claw.microsoft_oauth_manager as mm
        from captain_claw.microsoft_oauth import MicrosoftOAuthTokens
        from captain_claw.tools.microsoft_mail import MicrosoftMailTool

        async def tokens(self):
            return MicrosoftOAuthTokens(access_token="T", refresh_token="", scope="Mail.Read User.Read",
                                        expires_at=time.time() + 3000)
        monkeypatch.setattr(mm.MicrosoftOAuthManager, "access_token", tokens)
        graph = _Graph({})
        tool = MicrosoftMailTool()
        tool._client = _RealAsyncClient(transport=httpx.MockTransport(graph))
        res = await tool.execute("archive", message_ids=["M1"])
        assert not res.success and "mail organizing access (Mail.ReadWrite)" in res.error
        assert graph.requests == []

    async def test_archive_moves_to_the_archive_folder_and_prints_the_new_id(self, monkeypatch, tmp_path):
        graph = _Graph({_ARCHIVE: _ARCHIVE_OK, ("POST", "/me/messages/M1/move"): (201, {"id": "N1"})})
        res = await _mail(graph, monkeypatch).execute(
            "archive", message_id="M1", attachments=[str(tmp_path / "ignored.pdf")])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "ARCH"}]
        assert res.content.startswith("Archived 1 message to 'Archive'.")
        assert "Moving changes an Outlook message's ID" in res.content and "  M1 → N1" in res.content
        assert "Undo: microsoft_mail action=move folder=Inbox message_ids=[N1]" in res.content

    async def test_archive_without_an_archive_folder_is_refused(self, monkeypatch):
        graph = _Graph({("POST", "/me/messages/M1/move"): (201, {"id": "N1"})})
        res = await _mail(graph, monkeypatch).execute("archive", message_ids=["M1"])
        assert not res.success
        assert res.error == (
            "This mailbox has no Archive folder. Create one with action=create_folder "
            "folder=Archive and move there, or tell the user. Nothing was moved.")
        assert _posts(graph) == []

    async def test_archive_a_conversation_leaves_sent_drafts_and_archived_mail(self, monkeypatch):
        def known(fid, name):
            return 200, {"id": fid, "displayName": name}
        graph = _Graph({
            _ARCHIVE: _ARCHIVE_OK,
            ("GET", "/me/mailFolders/sentitems"): known("SENT", "Sent Items"),
            ("GET", "/me/mailFolders/drafts"): known("DRAFTS", "Drafts"),
            ("GET", "/me/mailFolders/deleteditems"): known("DEL", "Deleted Items"),
            ("GET", "/me/mailFolders/junkemail"): known("JUNK", "Junk Email"),
            # no Outbox in this mailbox (404)
            ("GET", "/me/messages"): (200, {"value": [
                {"id": "M1", "parentFolderId": "INBOX"}, {"id": "M2", "parentFolderId": "SENT"},
                {"id": "M3", "parentFolderId": "DRAFTS"}, {"id": "M4", "parentFolderId": "ARCH"},
                {"id": "M5", "parentFolderId": "INBOX"}, {"id": "M6", "parentFolderId": "DEL"},
                {"id": "M7", "parentFolderId": "JUNK"}]}),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
            ("POST", "/me/messages/M5/move"): (201, {"id": "N5"}),
        })
        res = await _mail(graph, monkeypatch).execute("archive", thread_ids="C1")
        assert res.success, res.error
        conv = next(r for r in graph.requests if r.url.path.endswith("/me/messages"))
        assert conv.url.params["$filter"] == "conversationId eq 'C1'"
        assert [r.url.path.rsplit("/", 2)[-2] for r in _posts(graph)] == ["M1", "M5"]
        assert "Archived 2 messages to 'Archive'." in res.content
        assert "M1 → N1" in res.content and "M5 → N5" in res.content
        assert ("Skipped: 1 message already in 'Archive'; left where they are: 1 in Sent Items, "
                "1 in Drafts, 1 in Deleted Items, 1 in Junk Email.") in res.content

    async def test_archive_reports_a_failure_and_still_succeeds(self, monkeypatch):
        graph = _Graph({_ARCHIVE: _ARCHIVE_OK, ("POST", "/me/messages/M1/move"): (201, {"id": "N1"})})
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("archive", message_ids=["M1", "M9", "M1"])
        assert res.success, res.error
        assert len(_posts(graph)) == 2  # de-duplicated
        assert "M1 → N1" in res.content and "  NOT archived: M9 (not found)" in res.content
        assert "message_ids=[N1]" in res.content
        res = await tool.execute("archive", message_ids=["M9"])
        assert not res.success
        assert res.error.startswith("Nothing was archived:\n  NOT archived: M9 (not found)")

    async def test_move_to_a_folder_by_display_name(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({"Receipts": "R1"}),
            ("GET", "/me/mailFolders/R1"): (200, {"id": "R1", "displayName": "Receipts"}),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder="Receipts", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "R1"}]
        assert "Moved 1 message to 'Receipts'." in res.content and "M1 → N1" in res.content

    async def test_move_to_a_parent_child_path(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({"Projects": "P1"}),
            ("GET", "/me/mailFolders/P1/childFolders"): _by_name({"Acme": "A1"}),
            ("GET", "/me/mailFolders/A1"): (200, {"id": "A1", "displayName": "Acme"}),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder="Projects / Acme", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "A1"}]

    async def test_move_to_an_inbox_subfolder_by_its_bare_name(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({"Receipts": "R2"}),
            ("GET", "/me/mailFolders/R2"): (200, {"id": "R2", "displayName": "Receipts"}),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder="Receipts", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "R2"}]

    async def test_move_to_a_missing_folder_changes_nothing(self, monkeypatch):
        graph = _Graph({("GET", "/me/mailFolders"): _by_name({}),
                        ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({})})
        res = await _mail(graph, monkeypatch).execute("move", folder="Nope", message_ids=["M1"])
        assert not res.success and "No mail folder named 'Nope'" in res.error
        assert "'Parent/Child'" in res.error and "Nothing was moved." in res.error
        assert _posts(graph) == []

    @pytest.mark.parametrize("folder", ["Deleted Items", "junk", "trash", "Sent", "drafts"])
    async def test_move_never_deletes_or_junks(self, monkeypatch, folder):
        graph = _Graph({("POST", "/me/messages/M1/move"): (201, {"id": "N1"})})
        res = await _mail(graph, monkeypatch).execute("move", folder=folder, message_ids=["M1"])
        assert not res.success and "doesn't move email to" in res.error
        assert "Nothing was moved." in res.error and graph.requests == []

    async def test_move_to_junk_by_its_display_name_is_refused(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({"Junk Email": "J1"}),
            ("GET", "/me/mailFolders/J1"): (200, {"id": "J1", "displayName": "Junk Email"}),
            ("GET", "/me/mailFolders/junkemail"): (200, {"id": "J1", "displayName": "Junk Email"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder="Junk Email", message_ids=["M1"])
        assert not res.success and "doesn't move email to Junk Email" in res.error
        assert _posts(graph) == []

    async def test_move_to_archive_falls_back_to_a_folder_named_archive(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({"Archive": "UA1"}),
            ("GET", "/me/mailFolders/UA1"): (200, {"id": "UA1", "displayName": "Archive"}),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder="Archive", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "UA1"}]

    async def test_create_a_top_level_folder_then_move_there(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("POST", "/me/mailFolders"): (201, {"id": "F1", "displayName": "Receipts"}),
            ("GET", "/me/mailFolders/F1"): (200, {"id": "F1", "displayName": "Receipts"}),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("create_folder", folder="Receipts")
        assert res.success, res.error
        assert res.content.startswith("Created folder 'Receipts'.\n  Folder ID: F1")
        assert graph.bodies("POST") == [{"displayName": "Receipts"}]
        lookups = len(graph.requests)
        res = await tool.execute("move", folder="receipts", message_ids=["M1"])  # from the cache
        assert res.success, res.error
        assert not any(r.url.params.get("$filter") for r in graph.requests[lookups:])
        assert graph.bodies("POST")[-1] == {"destinationId": "F1"}

    async def test_create_a_nested_folder_creates_its_parent(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({}),
            ("POST", "/me/mailFolders"): (201, {"id": "P1", "displayName": "Projects"}),
            ("POST", "/me/mailFolders/P1/childFolders"): (200, {"id": "A1", "displayName": "Acme"}),
        })
        res = await _mail(graph, monkeypatch).execute("create_folder", folder="Projects/Acme")
        assert res.success, res.error
        assert graph.bodies("POST") == [{"displayName": "Projects"}, {"displayName": "Acme"}]
        assert "Created folder 'Projects/Acme'." in res.content and "Folder ID: A1" in res.content
        assert "Also created its parent folder: 'Projects' (ID: P1)" in res.content
        assert not any("childFolders" in r.url.path for r in graph.requests if r.method == "GET")

    async def test_create_a_folder_inside_the_inbox(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders/inbox"): (200, {"id": "INBOX", "displayName": "Inbox"}),
            ("GET", "/me/mailFolders/INBOX/childFolders"): _by_name({}),
            ("POST", "/me/mailFolders/INBOX/childFolders"): (201, {"id": "R1", "displayName": "Receipts"}),
        })
        res = await _mail(graph, monkeypatch).execute("create_folder", folder="Inbox/Receipts")
        assert res.success, res.error
        assert [r.url.path for r in _posts(graph)] == ["/v1.0/me/mailFolders/INBOX/childFolders"]
        assert "Created folder 'Inbox/Receipts'." in res.content and "Folder ID: R1" in res.content

    async def test_create_an_existing_folder_creates_nothing(self, monkeypatch):
        graph = _Graph({("GET", "/me/mailFolders"): _by_name({"Receipts": "R1"})})
        res = await _mail(graph, monkeypatch).execute("create_folder", folder="Receipts")
        assert res.success, res.error
        assert res.content == "Folder 'Receipts' already exists (ID: R1) — nothing created."
        assert _posts(graph) == []

    async def test_create_folder_conflict_reports_the_existing_one(self, monkeypatch):
        seen = {"n": 0}

        def children(request):
            seen["n"] += 1  # not there on the first look; made meanwhile
            return _by_name({"Acme": "A1"} if seen["n"] > 1 else {})(request)
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({"Projects": "P1"}),
            ("GET", "/me/mailFolders/P1/childFolders"): children,
            ("POST", "/me/mailFolders/P1/childFolders"): (409, {"error": {
                "code": "ErrorFolderExists", "message": "A folder with the specified name already exists."}}),
        })
        res = await _mail(graph, monkeypatch).execute("create_folder", folder="Projects/Acme")
        assert res.success, res.error
        assert res.content == "Folder 'Projects/Acme' already exists (ID: A1) — nothing created."

    async def test_create_folder_refuses_an_empty_part(self, monkeypatch):
        graph = _Graph({})
        res = await _mail(graph, monkeypatch).execute("create_folder", folder="Projects//Acme")
        assert not res.success and "Nothing was created." in res.error and graph.requests == []

    async def test_add_categories_merges_in_any_case_and_patches_only_changes(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/messages/M1"): (200, {"id": "M1", "categories": ["receipts"]}),
            ("GET", "/me/messages/M2"): (200, {"id": "M2", "categories": ["Blue"]}),
            ("PATCH", "/me/messages/M2"): (200, {"id": "M2"}),
        })
        res = await _mail(graph, monkeypatch).execute(
            "add_categories", message_ids=["M1", "M2"], categories="Receipts")
        assert res.success, res.error
        patches = [r for r in graph.requests if r.method == "PATCH"]
        assert [r.url.path for r in patches] == ["/v1.0/me/messages/M2"]
        assert graph.bodies("PATCH") == [{"categories": ["Blue", "Receipts"]}]
        assert graph.requests[0].url.params["$select"] == "categories"
        assert "Added category 'Receipts' to 1 message." in res.content
        assert "1 message already had it — unchanged." in res.content
        assert 'Undo: microsoft_mail action=remove_categories categories=["Receipts"] message_ids=[M2]' in res.content

    async def test_remove_categories_keeps_the_others(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/messages/M1"): (200, {"id": "M1", "categories": ["Receipts", "Blue"]}),
            ("PATCH", "/me/messages/M1"): (200, {"id": "M1"}),
        })
        res = await _mail(graph, monkeypatch).execute(
            "remove_categories", message_id="M1", categories=["RECEIPTS"])
        assert res.success, res.error
        assert graph.bodies("PATCH") == [{"categories": ["Blue"]}]
        assert "Removed category 'RECEIPTS' from 1 message." in res.content
        assert 'action=add_categories categories=["Receipts"] message_ids=[M1]' in res.content

    async def test_categories_on_a_conversation_reach_every_message(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/messages"): (200, {"value": [
                {"id": "M1", "categories": []}, {"id": "M2", "categories": ["Finance"]},
                {"id": "M3", "categories": ["x"]}]}),
            ("PATCH", "/me/messages/M1"): (200, {}),
            ("PATCH", "/me/messages/M3"): (200, {}),
        })
        res = await _mail(graph, monkeypatch).execute(
            "add_categories", thread_ids=["C1"], categories=["Finance"])
        assert res.success, res.error
        assert graph.requests[0].url.params["$select"] == "id,categories"
        assert graph.bodies("PATCH") == [{"categories": ["Finance"]}, {"categories": ["x", "Finance"]}]
        assert [r.method for r in graph.requests] == ["GET", "PATCH", "PATCH"]  # no per-message reads
        assert "Added category 'Finance' to 2 messages." in res.content

    async def test_categories_are_required(self, monkeypatch):
        graph = _Graph({})
        res = await _mail(graph, monkeypatch).execute("add_categories", message_ids=["M1"], categories=" , ")
        assert not res.success and "needs categories=" in res.error and graph.requests == []

    def test_followup_hint_offers_organizing_only_when_allowed(self):
        import captain_claw.tools.microsoft_mail as mm

        bullet = ("  • Archive / move / categorize — ONLY if the user asked: microsoft_mail "
                  "action=archive thread_ids=[<Conversation ID above>] (also move, create_folder, "
                  "add_categories, remove_categories)")
        assert bullet in mm._FOLLOWUP_ORGANIZE
        with ma.bound(ma.human("what's new in my inbox?")):
            assert mm._followup_hint() == mm._FOLLOWUP_HINT + mm._FOLLOWUP_ORGANIZE
        with ma.bound(ma.automated("cron", "summarize my inbox")):
            assert mm._followup_hint() == mm._FOLLOWUP_HINT_NO_REPLY
        with ma.bound(ma.automated("cron", "archive newsletters")):
            assert mm._followup_hint() == mm._FOLLOWUP_HINT_NO_REPLY + mm._FOLLOWUP_ORGANIZE

    async def test_list_folders_lists_a_folders_subfolders(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({"Projects": "P1"}),
            ("GET", "/me/mailFolders/P1/childFolders"): (200, {"value": [
                {"id": "A1", "displayName": "Acme", "unreadItemCount": 2, "totalItemCount": 5}]}),
        })
        res = await _mail(graph, monkeypatch).execute("list_folders", folder="Projects")
        assert res.success, res.error
        assert res.content.startswith("Subfolders of 'Projects' (1):")
        assert "Acme  (2 unread / 5 total)\n    ID: A1" in res.content
        assert "$filter" not in graph.requests[-1].url.params


# ── microsoft_calendar ───────────────────────────────────────────────


def _cal(graph, monkeypatch, **kw):
    from captain_claw.tools.microsoft_calendar import MicrosoftCalendarTool

    return _tool(MicrosoftCalendarTool, graph, monkeypatch, **kw)


_CREATED = (201, {"id": "E1", "subject": "Sync",
                  "start": {"dateTime": "2026-10-20T10:00:00", "timeZone": "Europe/Zagreb"},
                  "end": {"dateTime": "2026-10-20T11:00:00", "timeZone": "Europe/Zagreb"}})


class TestMicrosoftCalendar:
    async def test_attendees_need_notify_attendees(self, monkeypatch):
        graph = _Graph({("POST", "/me/calendar/events"): _CREATED})
        res = await _cal(graph, monkeypatch).execute(
            "create_event", summary="Sync", start="2026-10-20T10:00:00", attendees=["ana@x.co"])
        assert not res.success and "notify_attendees=true" in res.error and graph.requests == []

    async def test_invitations_in_an_automated_turn_are_refused(self, monkeypatch):
        graph = _Graph({("POST", "/me/calendar/events"): _CREATED})
        with ma.bound(ma.automated("cron", "schedule a sync with ana@x.co")):
            res = await _cal(graph, monkeypatch).execute(
                "create_event", summary="Sync", start="2026-10-20T10:00:00",
                attendees=["ana@x.co"], notify_attendees=True)
        assert not res.success and ma.is_refusal(res.error) and graph.requests == []

    async def test_create_with_timezone_keeps_local_time(self, monkeypatch):
        graph = _Graph({("POST", "/me/calendar/events"): _CREATED})
        res = await _cal(graph, monkeypatch).execute(
            "create_event", summary="Sync", start="2026-10-20T10:00:00+02:00",
            timezone="Europe/Zagreb", attendees=["ana@x.co"], notify_attendees=True)
        assert res.success, res.error
        body = graph.bodies("POST")[0]
        assert body["start"] == {"dateTime": "2026-10-20T10:00:00", "timeZone": "Europe/Zagreb"}
        assert body["end"] == {"dateTime": "2026-10-20T11:00:00", "timeZone": "Europe/Zagreb"}
        assert body["attendees"][0]["emailAddress"]["address"] == "ana@x.co"
        assert "Invitations sent to: ana@x.co" in res.content

    async def test_all_day_and_offset_to_utc(self, monkeypatch):
        from captain_claw.tools.microsoft_calendar import _times

        day = _times("2026-10-20", None, None)
        assert day["isAllDay"] and day["end"]["dateTime"] == "2026-10-21T00:00:00"
        timed = _times("2026-10-20T10:00:00+02:00", None, None)
        assert timed["start"] == {"dateTime": "2026-10-20T08:00:00", "timeZone": "UTC"}

    def test_rrule_conversion(self):
        from captain_claw.tools.microsoft_calendar import _recurrence

        weekly = _recurrence(["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=10"], "2026-10-19T09:00:00", "Europe/Zagreb")
        assert weekly["pattern"] == {"interval": 2, "type": "weekly",
                                     "daysOfWeek": ["monday", "wednesday"], "firstDayOfWeek": "monday"}
        assert weekly["range"]["type"] == "numbered" and weekly["range"]["numberOfOccurrences"] == 10
        monthly = _recurrence(["RRULE:FREQ=MONTHLY;BYDAY=-1FR;UNTIL=20271231"], "2026-10-30", None)
        assert monthly["pattern"]["type"] == "relativeMonthly" and monthly["pattern"]["index"] == "last"
        assert monthly["range"] == {"startDate": "2026-10-30", "recurrenceTimeZone": "UTC",
                                    "type": "endDate", "endDate": "2027-12-31"}
        with pytest.raises(ValueError):
            _recurrence(["RRULE:FREQ=HOURLY"], "2026-10-30", None)

    async def test_deleting_an_organized_meeting_needs_notify(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/events/E1"): (200, {"id": "E1", "subject": "Sync", "isOrganizer": True,
                                             "attendees": [{"emailAddress": {"address": "ana@x.co"}}]}),
            ("DELETE", "/me/events/E1"): (204, b""),
        })
        tool = _cal(graph, monkeypatch)
        res = await tool.execute("delete_event", event_id="E1")
        assert not res.success and "cancellation" in res.error
        assert not any(r.method == "DELETE" for r in graph.requests)
        res = await tool.execute("delete_event", event_id="E1", notify_attendees=True)
        assert res.success and "Cancellation sent to: ana@x.co" in res.content

    async def test_respond_without_notify_tells_nobody(self, monkeypatch):
        graph = _Graph({("POST", "/me/events/E1/decline"): (202, b"")})
        res = await _cal(graph, monkeypatch).execute("respond_event", event_id="E1", response="decline")
        assert res.success and graph.bodies("POST")[0] == {"sendResponse": False}

    async def test_list_uses_calendar_view(self, monkeypatch):
        graph = _Graph({("GET", "/me/calendar/calendarView"): (200, {"value": [{
            "id": "E1", "subject": "Sync", "isAllDay": False,
            "start": {"dateTime": "2026-10-20T08:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-20T09:00:00.0000000", "timeZone": "UTC"}}]})})
        res = await _cal(graph, monkeypatch).execute("list_events", max_results=5)
        assert "Sync" in res.content and "2026-10-20 08:00 → 09:00" in res.content
        req = graph.requests[0]
        assert "startDateTime" in req.url.params and req.headers["Prefer"].startswith('outlook.timezone="UTC"')

    async def test_read_scope_missing_is_explained(self, monkeypatch):
        from captain_claw.microsoft_graph import graph_token
        import captain_claw.microsoft_oauth_manager as mm
        from captain_claw.microsoft_oauth import MicrosoftOAuthTokens

        async def tokens(self):
            return MicrosoftOAuthTokens(access_token="T", refresh_token="", scope="Mail.Read User.Read",
                                        expires_at=time.time() + 3000)
        monkeypatch.setattr(mm.MicrosoftOAuthManager, "access_token", tokens)
        from captain_claw.microsoft_graph import CALENDAR_READ
        with pytest.raises(RuntimeError, match="Connections → Microsoft"):
            await graph_token(CALENDAR_READ, "calendar access")


# ── microsoft_onedrive ───────────────────────────────────────────────


def _drive(graph, monkeypatch, **kw):
    from captain_claw.tools.microsoft_onedrive import MicrosoftOneDriveTool

    return _tool(MicrosoftOneDriveTool, graph, monkeypatch, **kw)


_ROOT = (200, {"id": "ROOT", "name": "root", "folder": {"childCount": 1},
               "parentReference": {"driveId": "DRV"}})


class TestMicrosoftOneDrive:
    def test_share_url_token(self):
        from captain_claw.tools.microsoft_onedrive import _share_token

        assert _share_token("https://onedrive.live.com/redir?resid=1234!567") == \
            "u!aHR0cHM6Ly9vbmVkcml2ZS5saXZlLmNvbS9yZWRpcj9yZXNpZD0xMjM0ITU2Nw"

    async def test_create_never_overwrites(self, monkeypatch):
        def put(request):
            assert request.url.params["@microsoft.graph.conflictBehavior"] == "rename"
            return (201, {"id": "F2", "name": "notes 1.md", "size": 5})
        graph = _Graph({
            ("GET", "/me/drive/root"): _ROOT,
            ("PUT", "/drives/DRV/items/ROOT:/notes.md:/content"): put,
        })
        res = await _drive(graph, monkeypatch).execute("create", name="notes.md", content="hello")
        assert res.success, res.error
        assert "saved as" not in res.content and "this one is 'notes 1.md'" in res.content

    async def test_text_update_over_a_docx_is_refused(self, monkeypatch):
        graph = _Graph({("GET", "/me/drive/items/F1"): (200, {
            "id": "F1", "name": "Plan.docx", "file": {}, "parentReference": {"driveId": "DRV"}})})
        res = await _drive(graph, monkeypatch).execute("update", file_id="F1", content="oops")
        assert not res.success and "binary document" in res.error
        assert not any(r.method == "PUT" for r in graph.requests)

    async def test_path_addressing_and_text_read(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/drive/root:/Docs/a b.txt"): (200, {
                "id": "F9", "name": "a b.txt", "size": 5, "file": {"mimeType": "text/plain"},
                "parentReference": {"driveId": "DRV"}}),
            ("GET", "/drives/DRV/items/F9/content"): (200, b"hello"),
        })
        res = await _drive(graph, monkeypatch).execute("read", path="/Docs/a b.txt")
        assert res.success, res.error
        assert res.content.endswith("hello") and "[OneDrive: a b.txt]" in res.content

    async def test_write_needs_a_write_scope(self, monkeypatch):
        from captain_claw.microsoft_graph import FILES_WRITE

        assert "files.read" not in FILES_WRITE and "files.readwrite.all" in FILES_WRITE

    async def test_shared_url_without_all_scope_explains(self, monkeypatch):
        graph = _Graph({})
        res = await _drive(graph, monkeypatch, scopes=("files.readwrite",)).execute(
            "read", file_id="https://contoso-my.sharepoint.com/:w:/g/personal/x/abc")
        assert not res.success and "read all you can open" in res.error
        assert graph.requests[0].url.path.startswith("/v1.0/shares/u!")
