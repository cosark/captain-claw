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


def _folder(fid: str, name: str, parent: str = "ROOT"):
    """A mailFolder answer (``$select=id,displayName,parentFolderId``)."""
    return 200, {"id": fid, "displayName": name, "parentFolderId": parent}


# The mailbox root and the well-known folders a move checks (no Archive, no Outbox).
_KNOWN = {
    ("GET", "/me/mailFolders/msgfolderroot"): _folder("ROOT", "Top of Information Store", "NONIPM"),
    ("GET", "/me/mailFolders/inbox"): _folder("INBOX", "Inbox"),
    ("GET", "/me/mailFolders/sentitems"): _folder("SENT", "Sent Items"),
    ("GET", "/me/mailFolders/drafts"): _folder("DRAFTS", "Drafts"),
    ("GET", "/me/mailFolders/deleteditems"): _folder("DEL", "Deleted Items"),
    ("GET", "/me/mailFolders/junkemail"): _folder("JUNK", "Junk Email"),
}
# A real Graph folder ID (base64, longer than any name).
_FOLDER_ID = ("AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OAAuAAAAAAAiQ8W967B7TKBjgx9rVEURAQAi"
              "IsqMbYjsT5e-T7KzowPTAAAAAAEMAAA=")


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
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("POST", "/me/mailFolders"): (201, {"id": "P1", "displayName": "Projects"}),
            ("POST", "/me/mailFolders/P1/childFolders"): (200, {"id": "A1", "displayName": "Acme"}),
        })
        res = await _mail(graph, monkeypatch).execute("create_folder", folder="Projects/Acme")
        assert res.success, res.error
        assert graph.bodies("POST") == [{"displayName": "Projects"}, {"displayName": "Acme"}]
        assert "Created folder 'Projects/Acme'." in res.content and "Folder ID: A1" in res.content
        assert "Also created its parent folder: 'Projects' (ID: P1)" in res.content
        # Projects was looked for at the top level and in the Inbox — never inside the one made just now.
        assert [r.url.path for r in graph.requests if r.method == "GET"] == [
            "/v1.0/me/mailFolders", "/v1.0/me/mailFolders/inbox/childFolders"]

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

    # ── folders inside Deleted Items / Junk Email ──

    @pytest.mark.parametrize("folder,name,bin_label", [
        ("Deleted Items/Old", "Old", "Deleted Items"),
        ("trash / Old / Deeper", "Deeper", "Deleted Items"),
        ("Junk/X", "X", "Junk Email"),
        (_FOLDER_ID, "X", "Junk Email"),  # by ID
    ])
    async def test_move_into_a_folder_inside_deleted_items_or_junk_is_refused(
        self, monkeypatch, folder, name, bin_label,
    ):
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders/deleteditems/childFolders"): _by_name({"Old": "OLD"}),
            ("GET", "/me/mailFolders/OLD/childFolders"): _by_name({"Deeper": "DEEP"}),
            ("GET", "/me/mailFolders/junkemail/childFolders"): _by_name({"X": _FOLDER_ID}),
            ("GET", "/me/mailFolders/OLD"): _folder("OLD", "Old", "DEL"),
            ("GET", "/me/mailFolders/DEEP"): _folder("DEEP", "Deeper", "OLD"),
            ("GET", f"/me/mailFolders/{_FOLDER_ID}"): _folder(_FOLDER_ID, "X", "JUNK"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder=folder, message_ids=["M1"])
        assert not res.success
        assert res.error.startswith(f"microsoft_mail doesn't move email to '{name}' (it's inside {bin_label})")
        assert "Nothing was moved." in res.error and _posts(graph) == []

    async def test_move_into_an_ordinary_subfolder_still_works(self, monkeypatch):
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({"Receipts": "R1"}),
            ("GET", "/me/mailFolders/R1/childFolders"): _by_name({"2026": "R26"}),
            ("GET", "/me/mailFolders/R26"): _folder("R26", "2026", "R1"),
            ("GET", "/me/mailFolders/R1"): _folder("R1", "Receipts", "INBOX"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("move", folder="Inbox/Receipts/2026", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "R26"}]
        walked = len(graph.requests)
        res = await tool.execute("move", folder="Inbox/Receipts/2026", message_ids=["M1"])
        assert res.success, res.error
        # The folder comes from the cache (no lookup by name); where its
        # parents are is read afresh, then the move.
        again = graph.requests[walked:]
        assert not any(r.url.params.get("$filter") for r in again)
        assert "/v1.0/me/mailFolders/R1" in [r.url.path for r in again]
        assert again[-1].method == "POST" and len(_posts(graph)) == 2

    async def test_a_folder_deleted_since_it_was_cached_is_refused(self, monkeypatch):
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders"): _by_name({"Receipts": "R1"}),
            ("GET", "/me/mailFolders/R1"): _folder("R1", "Receipts"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        assert (await tool.execute("move", folder="Receipts", message_ids=["M1"])).success
        # Deleting it in Outlook moves it (same ID) into Deleted Items.
        graph.routes[("GET", "/me/mailFolders/R1")] = _folder("R1", "Receipts", "DEL")
        res = await tool.execute("move", folder="Receipts", message_ids=["M1"])
        assert not res.success
        assert res.error.startswith("microsoft_mail doesn't move email to 'Receipts' (it's inside Deleted Items)")
        assert len(_posts(graph)) == 1

    @pytest.mark.parametrize("folder", ["Projects/Acme", _FOLDER_ID])  # by name (cached) / by ID
    async def test_a_move_reads_afresh_where_the_destinations_parents_are(self, monkeypatch, folder):
        top = {"Projects": "P1"}
        where = {"P1": "ROOT"}
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders"): _by_name(top),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("GET", "/me/mailFolders/P1/childFolders"): _by_name({"Acme": _FOLDER_ID}),
            ("GET", f"/me/mailFolders/{_FOLDER_ID}"): _folder(_FOLDER_ID, "Acme", "P1"),
            ("GET", "/me/mailFolders/P1"): lambda r: _folder("P1", "Projects", where["P1"]),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("move", folder=folder, message_ids=["M1"])
        assert res.success, res.error
        # The user deletes Projects in Outlook: it moves (same IDs) into Deleted Items.
        where["P1"] = "DEL"
        top.clear()
        res = await tool.execute("move", folder=folder, message_ids=["M1"])
        assert not res.success and res.error.endswith("Nothing was moved.")
        if folder == _FOLDER_ID:
            assert res.error.startswith("microsoft_mail doesn't move email to 'Acme' (it's inside Deleted Items)")
        else:  # the name is looked up again, as a new session would: there is no such folder now
            assert res.error.startswith("No mail folder named 'Projects/Acme'.")
        assert len(_posts(graph)) == 1

    async def test_a_cached_folder_deleted_into_deleted_items_gives_way_to_a_live_one(self, monkeypatch):
        top = {"Receipts": "R1"}
        where = {"R1": "ROOT", "R2": "ROOT"}

        def mail(subject):
            return 200, {"value": [{"id": "m-" + subject, "conversationId": "c1", "subject": subject,
                                    "isRead": True}]}
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders"): _by_name(top),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("GET", "/me/mailFolders/R1"): lambda r: _folder("R1", "Receipts", where["R1"]),
            ("GET", "/me/mailFolders/R2"): lambda r: _folder("R2", "Receipts", where["R2"]),
            ("GET", "/me/mailFolders/R1/messages"): mail("Old receipt"),
            ("GET", "/me/mailFolders/R2/messages"): mail("New receipt"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("list_messages", folder="Receipts")
        assert res.success and "Old receipt" in res.content
        # Deleted in Outlook (into Deleted Items, ID kept) and made again.
        where["R1"] = "DEL"
        top["Receipts"] = "R2"
        res = await tool.execute("list_messages", folder="Receipts")
        assert res.success, res.error
        assert "New receipt" in res.content and "Old receipt" not in res.content
        res = await tool.execute("move", folder="Receipts", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "R2"}]
        # Deleted again, with no live folder of that name: as in a new session.
        where["R2"] = "DEL"
        top.clear()
        res = await tool.execute("list_messages", folder="Receipts")
        assert not res.success and res.error.startswith("No mail folder named 'Receipts'.")
        res = await tool.execute("move", folder="Receipts", message_ids=["M1"])
        assert not res.success and res.error.endswith("Nothing was moved.")
        assert len(_posts(graph)) == 1
        assert sum(r.url.path == "/v1.0/me/mailFolders/R1/messages" for r in graph.requests) == 1

    async def test_junk_email_named_by_its_display_name_stays_cached(self, monkeypatch):
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders"): _by_name({"Junk Email": "JUNK"}),
            ("GET", "/me/mailFolders/JUNK/messages"): _EMPTY,
        })
        tool = _mail(graph, monkeypatch)
        for _ in range(2):
            res = await tool.execute("list_messages", folder="Junk Email")
            assert res.success and res.content == "No messages in Junk Email."
        # It is Junk Email — not a folder deleted since: looked up once.
        assert sum(bool(r.url.params.get("$filter")) for r in graph.requests) == 1

    @pytest.mark.parametrize("folder", ["Deleted Items/Bin", "Junk Email/Old/Bin"])  # an alias / its name
    async def test_create_folder_inside_deleted_items_or_junk_is_refused(self, monkeypatch, folder):
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders"): _by_name({"Junk Email": "JUNK"}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("GET", "/me/mailFolders/DEL/childFolders"): _by_name({}),
            ("GET", "/me/mailFolders/JUNK/childFolders"): _by_name({"Old": "JOLD"}),
            ("GET", "/me/mailFolders/JOLD/childFolders"): _by_name({}),
            ("POST", "/me/mailFolders/DEL/childFolders"): (201, {"id": "BIN"}),
            ("POST", "/me/mailFolders/JOLD/childFolders"): (201, {"id": "BIN"}),
        })
        res = await _mail(graph, monkeypatch).execute("create_folder", folder=folder)
        assert not res.success
        assert res.error.startswith("microsoft_mail doesn't make folders inside ")
        assert "Nothing was created." in res.error and _posts(graph) == []

    async def test_a_conversation_move_leaves_mail_inside_deleted_items_subfolders(self, monkeypatch):
        graph = _Graph({
            **_KNOWN, _ARCHIVE: _ARCHIVE_OK,
            ("GET", "/me/messages"): (200, {"value": [
                {"id": "M1", "parentFolderId": "INBOX"}, {"id": "M2", "parentFolderId": "OLD"},
                {"id": "M3", "parentFolderId": "ACME"}, {"id": "M4", "parentFolderId": "DEEP"},
                {"id": "M5", "parentFolderId": "JX"}]}),
            ("GET", "/me/mailFolders/OLD"): _folder("OLD", "Old", "DEL"),
            ("GET", "/me/mailFolders/DEEP"): _folder("DEEP", "Deeper", "OLD"),
            ("GET", "/me/mailFolders/JX"): _folder("JX", "X", "JUNK"),
            ("GET", "/me/mailFolders/ACME"): _folder("ACME", "Acme", "CLIENTS"),
            ("GET", "/me/mailFolders/CLIENTS"): _folder("CLIENTS", "Clients"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
            ("POST", "/me/messages/M3/move"): (201, {"id": "N3"}),
        })
        res = await _mail(graph, monkeypatch).execute("archive", thread_ids=["C1"])
        assert res.success, res.error
        assert sorted(r.url.path.rsplit("/", 2)[-2] for r in _posts(graph)) == ["M1", "M3"]
        assert "Skipped: left where they are: 2 in Deleted Items, 1 in Junk Email." in res.content

    @pytest.mark.parametrize("parent, routes, why", [
        # 40 levels below Deleted Items: deeper than the walk goes.
        ("F39", {("GET", f"/me/mailFolders/F{i}"): _folder(f"F{i}", f"L{i}", f"F{i - 1}" if i else "DEL")
                 for i in range(40)}, "go too deep"),
        # A parent that is gone (the mailbox root is known, so it isn't the top).
        ("GONE", {}, "is gone"),
        # Parents that loop.
        ("F1", {("GET", "/me/mailFolders/F1"): _folder("F1", "L1", "F2"),
                ("GET", "/me/mailFolders/F2"): _folder("F2", "L2", "F1")}, "loop"),
    ])
    async def test_a_destination_whose_place_cant_be_told_is_refused(
        self, monkeypatch, parent, routes, why,
    ):
        graph = _Graph({
            **_KNOWN, **routes,
            ("GET", f"/me/mailFolders/{_FOLDER_ID}"): _folder(_FOLDER_ID, "Deep", parent),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder=_FOLDER_ID, message_ids=["M1"])
        assert not res.success
        assert res.error.startswith("Couldn't confirm that ")
        assert why in res.error and "nothing was moved" in res.error
        assert _posts(graph) == []

    async def test_a_conversation_message_whose_folder_cant_be_placed_stays(self, monkeypatch):
        graph = _Graph({
            **_KNOWN, _ARCHIVE: _ARCHIVE_OK,
            ("GET", "/me/messages"): (200, {"value": [
                {"id": "M1", "parentFolderId": "INBOX"}, {"id": "M2", "parentFolderId": "ORPHAN"}]}),
            ("GET", "/me/mailFolders/ORPHAN"): _folder("ORPHAN", "Old stuff", "GONE"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
            ("POST", "/me/messages/M2/move"): (201, {"id": "N2"}),
        })
        res = await _mail(graph, monkeypatch).execute("archive", thread_ids=["C1"])
        assert res.success, res.error
        assert [r.url.path.rsplit("/", 2)[-2] for r in _posts(graph)] == ["M1"]
        assert "NOT archived: M2 (couldn't check its folder — one of its parent folders is gone)" in res.content

    # ── finding folders ──

    async def test_a_path_under_an_inbox_subfolder(self, monkeypatch):
        children = {"Acme": "AIN"}
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({}),  # no top-level Projects
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({"Projects": "PIN"}),
            ("GET", "/me/mailFolders/PIN/childFolders"): _by_name(children),
            ("GET", "/me/mailFolders/AIN/messages"): (200, {"value": [
                {"id": "m1", "conversationId": "c1", "subject": "Kickoff", "isRead": True}]}),
            ("GET", "/me/mailFolders/AIN"): _folder("AIN", "Acme", "PIN"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
            ("POST", "/me/mailFolders/PIN/childFolders"): (201, {"id": "BETA", "displayName": "Beta"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("list_messages", folder="Projects/Acme")
        assert res.success, res.error
        assert "Kickoff" in res.content
        res = await tool.execute("move", folder="Projects/Acme", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "AIN"}]
        res = await tool.execute("create_folder", folder="Projects/Acme")
        assert res.success, res.error
        assert res.content == "Folder 'Projects/Acme' already exists in the Inbox (ID: AIN) — nothing created."
        assert len(_posts(graph)) == 1
        res = await tool.execute("create_folder", folder="Projects/Beta")
        assert res.success, res.error
        assert res.content.startswith("Created folder 'Projects/Beta' in the Inbox.\n  Folder ID: BETA")
        assert [r.url.path for r in _posts(graph)][1:] == ["/v1.0/me/mailFolders/PIN/childFolders"]
        assert "Also created" not in res.content  # no second, top-level Projects

    async def test_list_and_search_find_inbox_subfolders_and_paths(self, monkeypatch):
        msgs = (200, {"value": [{"id": "m1", "conversationId": "c1", "subject": "Invoice", "isRead": True,
                                 "receivedDateTime": "2026-10-01T08:00:00Z"}]})
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({"Receipts": "R2"}),
            ("GET", "/me/mailFolders/R2/messages"): msgs,
        })
        for folder in ("Receipts", "Inbox/Receipts"):
            for action, extra in (("list_messages", {}), ("search", {"query": "invoice"})):
                res = await _mail(graph, monkeypatch).execute(action, folder=folder, **extra)
                assert res.success, res.error
                assert "Invoice" in res.content
                assert graph.requests[-1].url.path == "/v1.0/me/mailFolders/R2/messages"

    async def test_a_mailbox_without_an_archive_folder_reaches_one_named_archive(self, monkeypatch):
        msgs = (200, {"value": [{"id": "m1", "conversationId": "c1", "subject": "Old news", "isRead": True}]})
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({"Archive": "UA1"}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("GET", "/me/mailFolders/UA1/childFolders"): _by_name({"2024": "Y24"}),
            ("GET", "/me/mailFolders/UA1/messages"): msgs,
            ("GET", "/me/mailFolders/Y24"): _folder("Y24", "2024", "UA1"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })  # GET /me/mailFolders/archive: 404
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("list_messages", folder="Archive")
        assert res.success, res.error
        assert "Old news" in res.content
        res = await tool.execute("search", folder="archive", query="news")
        assert res.success and "Old news" in res.content
        res = await tool.execute("list_folders", folder="Archive")
        assert res.success, res.error
        assert res.content.startswith("Subfolders of 'Archive' (1):") and "ID: Y24" in res.content
        res = await tool.execute("move", folder="Archive/2024", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "Y24"}]
        # The well-known Archive was tried once; then the user's folder came from the cache.
        assert sum(r.url.path == "/v1.0/me/mailFolders/archive/messages" for r in graph.requests) == 1

    async def test_a_folder_created_for_archiving_is_listed_by_name(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("POST", "/me/mailFolders"): (201, {"id": "UA1", "displayName": "Archive"}),
            ("GET", "/me/mailFolders/UA1/messages"): _EMPTY,
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("archive", message_ids=["M1"])
        assert not res.success and "create_folder folder=Archive" in res.error
        res = await tool.execute("create_folder", folder="Archive")
        assert res.success and res.content.startswith("Created folder 'Archive'.")
        res = await tool.execute("list_messages", folder="Archive")
        assert res.success and res.content == "No messages in Archive."
        assert graph.requests[-1].url.path == "/v1.0/me/mailFolders/UA1/messages"

    async def test_a_folder_name_with_a_slash(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({"Invoices 2024/25": "FS"}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("GET", "/me/mailFolders/FS/messages"): _EMPTY,
            ("GET", "/me/mailFolders/FS"): _folder("FS", "Invoices 2024/25"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("list_messages", folder="Invoices 2024/25")
        assert res.success, res.error
        assert graph.requests[-1].url.path == "/v1.0/me/mailFolders/FS/messages"
        res = await _mail(graph, monkeypatch).execute("move", folder="Invoices 2024/25", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "FS"}]

    async def test_move_by_folder_id(self, monkeypatch):
        graph = _Graph({
            ("GET", f"/me/mailFolders/{_FOLDER_ID}"): _folder(_FOLDER_ID, "Receipts"),
            ("GET", f"/me/mailFolders/{_FOLDER_ID}/messages"): _EMPTY,
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("move", folder=_FOLDER_ID, message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": _FOLDER_ID}]
        assert "Moved 1 message to 'Receipts'." in res.content
        res = await tool.execute("list_messages", folder=_FOLDER_ID)
        assert res.success, res.error
        assert graph.requests[-1].url.path == f"/v1.0/me/mailFolders/{_FOLDER_ID}/messages"
        assert not any(r.url.params.get("$filter") for r in graph.requests)  # never looked up by name

    async def test_a_long_one_word_folder_name_is_not_taken_for_an_id(self, monkeypatch):
        name = "Newsletters_and_marketing_updates_from_vendors"  # 46 characters
        malformed = (400, {"error": {"code": "ErrorInvalidIdMalformed", "message": "Id is malformed."}})
        graph = _Graph({
            ("GET", f"/me/mailFolders/{name}"): malformed,
            ("GET", f"/me/mailFolders/{name}/messages"): malformed,
            ("GET", "/me/mailFolders"): _by_name({name: "NL"}),
            ("GET", "/me/mailFolders/NL"): _folder("NL", name),
            ("GET", "/me/mailFolders/NL/messages"): _EMPTY,
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder=name, message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST") == [{"destinationId": "NL"}]
        res = await _mail(graph, monkeypatch).execute("list_messages", folder=name)
        assert res.success and res.content == f"No messages in {name}."

    async def test_a_long_folder_name_create_folder_made_is_found_from_the_cache(self, monkeypatch):
        name = "Receipts_and_Invoices_2026_Accounting_Dept"  # 42 characters
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name({}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("POST", "/me/mailFolders"): (201, {"id": "F1", "displayName": name}),
            ("GET", "/me/mailFolders/F1"): _folder("F1", name),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("create_folder", folder=name)
        assert res.success, res.error
        assert f"microsoft_mail action=move folder='{name}'" in res.content
        res = await tool.execute("move", folder=name, message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST")[-1] == {"destinationId": "F1"}
        assert not any(r.url.path.endswith(name) for r in graph.requests)

    async def test_a_cached_folder_since_deleted_is_looked_up_again(self, monkeypatch):
        folders = {"Receipts": "R1"}
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name(folders),
            ("GET", "/me/mailFolders/R1"): _folder("R1", "Receipts"),
            ("GET", "/me/mailFolders/R2"): _folder("R2", "Receipts"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("move", folder="Receipts", message_ids=["M1"])
        assert res.success and graph.bodies("POST") == [{"destinationId": "R1"}]
        # The user deletes Receipts in Outlook and makes it again.
        del graph.routes[("GET", "/me/mailFolders/R1")]
        folders["Receipts"] = "R2"
        res = await tool.execute("move", folder="Receipts", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST")[-1] == {"destinationId": "R2"}

    async def test_create_folder_refreshes_the_cache_with_the_folder_it_found(self, monkeypatch):
        folders = {"Receipts": "R1"}
        graph = _Graph({
            ("GET", "/me/mailFolders"): _by_name(folders),
            ("GET", "/me/mailFolders/R1"): _folder("R1", "Receipts"),
            ("GET", "/me/mailFolders/R2"): _folder("R2", "Receipts"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        assert (await tool.execute("move", folder="Receipts", message_ids=["M1"])).success
        del graph.routes[("GET", "/me/mailFolders/R1")]
        folders["Receipts"] = "R2"
        res = await tool.execute("create_folder", folder="Receipts")
        assert res.content == "Folder 'Receipts' already exists (ID: R2) — nothing created."
        seen = len(graph.requests)
        res = await tool.execute("move", folder="Receipts", message_ids=["M1"])
        assert res.success, res.error
        assert graph.bodies("POST")[-1] == {"destinationId": "R2"}
        assert graph.requests[seen].url.path == "/v1.0/me/mailFolders/R2"  # straight from the cache

    async def test_create_a_top_level_folder_conflict_reports_the_existing_one(self, monkeypatch):
        seen = {"n": 0}

        def top(request):
            seen["n"] += 1  # not there on the first look; made meanwhile
            return _by_name({"Receipts": "R1"} if seen["n"] > 1 else {})(request)
        graph = _Graph({
            ("GET", "/me/mailFolders"): top,
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({}),
            ("POST", "/me/mailFolders"): (409, {"error": {
                "code": "ErrorFolderExists", "message": "A folder with the specified name already exists."}}),
        })
        res = await _mail(graph, monkeypatch).execute("create_folder", folder="Receipts")
        assert res.success, res.error
        assert res.content == "Folder 'Receipts' already exists (ID: R1) — nothing created."

    async def test_create_folder_leaves_what_a_shorter_name_means(self, monkeypatch):
        # A top-level Projects/Acme, and Inbox/Projects/Acme/Q1.
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders"): _by_name({"Projects": "PTOP"}),
            ("GET", "/me/mailFolders/PTOP/childFolders"): _by_name({"Acme": "ATOP"}),
            ("GET", "/me/mailFolders/ATOP/childFolders"): _by_name({}),
            ("GET", "/me/mailFolders/inbox/childFolders"): _by_name({"Projects": "PIN"}),
            ("GET", "/me/mailFolders/PIN/childFolders"): _by_name({"Acme": "AIN"}),
            ("GET", "/me/mailFolders/AIN/childFolders"): _by_name({"Q1": "Q1IN"}),
            ("GET", "/me/mailFolders/PTOP"): _folder("PTOP", "Projects"),
            ("GET", "/me/mailFolders/ATOP"): _folder("ATOP", "Acme", "PTOP"),
            ("GET", "/me/mailFolders/PIN"): _folder("PIN", "Projects", "INBOX"),
            ("GET", "/me/mailFolders/AIN"): _folder("AIN", "Acme", "PIN"),
            ("GET", "/me/mailFolders/Q1IN"): _folder("Q1IN", "Q1", "AIN"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("create_folder", folder="Projects/Acme/Q1")
        assert res.content == "Folder 'Projects/Acme/Q1' already exists in the Inbox (ID: Q1IN) — nothing created."
        for folder, dest in (("Projects", "PTOP"), ("Projects/Acme", "ATOP"), ("Projects/Acme/Q1", "Q1IN")):
            res = await tool.execute("move", folder=folder, message_ids=["M1"])
            assert res.success, res.error
            assert graph.bodies("POST")[-1] == {"destinationId": dest}, folder

    # ── what a move answers ──

    async def test_a_conversation_moves_undo_names_the_folder_each_came_from(self, monkeypatch):
        graph = _Graph({
            **_KNOWN, _ARCHIVE: _ARCHIVE_OK,
            ("GET", "/me/messages"): (200, {"value": [
                {"id": "M1", "parentFolderId": "INBOX"}, {"id": "M2", "parentFolderId": "ACME"}]}),
            ("GET", "/me/mailFolders/ACME"): _folder("ACME", "Acme"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
            ("POST", "/me/messages/M2/move"): (201, {"id": "N2"}),
        })
        res = await _mail(graph, monkeypatch).execute("archive", thread_ids=["C1"])
        assert res.success, res.error
        assert "Undo: microsoft_mail action=move folder=Inbox message_ids=[N1]" in res.content
        assert "Undo: microsoft_mail action=move folder=ACME message_ids=[N2]" in res.content
        assert "came from" not in res.content

    async def test_undo_after_many_moves_from_several_folders_counts_each(self, monkeypatch):
        msgs = [{"id": f"M{i}", "parentFolderId": "ACME" if i < 3 else "INBOX"} for i in range(25)]
        graph = _Graph({
            **_KNOWN, _ARCHIVE: _ARCHIVE_OK,
            ("GET", "/me/messages"): (200, {"value": msgs}),
            ("GET", "/me/mailFolders/ACME"): _folder("ACME", "Acme"),
            **{("POST", f"/me/messages/M{i}/move"): (201, {"id": f"N{i}"}) for i in range(25)},
        })
        res = await _mail(graph, monkeypatch).execute("archive", thread_ids=["C1"])
        assert res.success, res.error
        undo = res.content.split("\nUndo", 1)[1]
        assert undo == (
            " — their new IDs aren't all listed here; list_messages folder='Archive' shows them. "
            "By the folder they came from:\n"
            "  3 messages from ACME: microsoft_mail action=move folder=ACME message_ids=[their new IDs]\n"
            "  22 messages from Inbox: microsoft_mail action=move folder=Inbox message_ids=[their new IDs]")

    async def test_a_conversation_move_where_one_folder_cant_be_checked_moves_the_rest(self, monkeypatch):
        busy = (503, {"error": {"code": "ServiceUnavailable", "message": "busy"}})
        graph = _Graph({
            **_KNOWN, _ARCHIVE: _ARCHIVE_OK,
            ("GET", "/me/messages"): (200, {"value": [
                {"id": "M1", "parentFolderId": "INBOX"}, {"id": "M2", "parentFolderId": "UF"},
                {"id": "M3", "parentFolderId": "ACME"}]}),
            ("GET", "/me/mailFolders/UF"): busy,
            ("GET", "/me/mailFolders/ACME"): _folder("ACME", "Acme"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
            ("POST", "/me/messages/M3/move"): (201, {"id": "N3"}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("archive", thread_ids=["C1"])
        assert res.success, res.error
        assert sorted(r.url.path.rsplit("/", 2)[-2] for r in _posts(graph)) == ["M1", "M3"]
        assert "Archived 2 messages to 'Archive'." in res.content
        assert "  NOT archived: M2 (couldn't check its folder — Outlook HTTP 503: busy)" in res.content
        # Only that one: nothing moved, and not a success.
        graph.routes[("GET", "/me/messages")] = (200, {"value": [{"id": "M2", "parentFolderId": "UF"}]})
        res = await tool.execute("archive", thread_ids=["C1"])
        assert not res.success
        assert res.error == ("Nothing was archived:\n  NOT archived: M2 (couldn't check its folder — "
                             "Outlook HTTP 503: busy)")
        assert len(_posts(graph)) == 2

    @pytest.mark.parametrize("folder", ["Posteingang", "INBOXID" * 8])
    async def test_a_move_into_the_inbox_by_its_id_or_local_name_undoes_to_the_source(
        self, monkeypatch, folder,
    ):
        graph = _Graph({
            **_KNOWN,
            ("GET", "/me/mailFolders/inbox"): _folder("INBOXID" * 8, "Posteingang"),
            ("GET", "/me/mailFolders"): _by_name({"Posteingang": "INBOXID" * 8}),
            ("GET", f"/me/mailFolders/{'INBOXID' * 8}"): _folder("INBOXID" * 8, "Posteingang"),
            ("POST", "/me/messages/M1/move"): (201, {"id": "N1"}),
        })
        res = await _mail(graph, monkeypatch).execute("move", folder=folder, message_ids=["M1"])
        assert res.success, res.error
        assert "Undo: microsoft_mail action=move folder=<the folder they came from> message_ids=[N1]" in res.content
        assert "folder=Inbox" not in res.content

    async def test_a_move_where_every_try_failed_is_not_a_success(self, monkeypatch):
        graph = _Graph({
            **_KNOWN, _ARCHIVE: _ARCHIVE_OK,
            ("GET", "/me/messages"): (200, {"value": [
                {"id": "M1", "parentFolderId": "INBOX"}, {"id": "M2", "parentFolderId": "SENT"}]}),
            ("POST", "/me/messages/M1/move"): (429, {"error": {"code": "TooManyRequests", "message": "slow down"}}),
        })
        res = await _mail(graph, monkeypatch).execute("archive", thread_ids=["C1"])
        assert not res.success
        assert res.error == ("Nothing was archived:\n  NOT archived: M1 (Outlook HTTP 429: slow down)\n"
                             "  Skipped: left where they are: 1 in Sent Items.")

    async def test_categories_where_every_change_failed_is_not_a_success(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/messages/M1"): (200, {"id": "M1", "categories": ["Receipts"]}),
            ("GET", "/me/messages/M2"): (200, {"id": "M2", "categories": []}),
            ("PATCH", "/me/messages/M2"): (503, {"error": {"code": "ServiceUnavailable", "message": "busy"}}),
        })
        res = await _mail(graph, monkeypatch).execute(
            "add_categories", message_ids=["M1", "M2"], categories=["Receipts"])
        assert not res.success
        assert res.error == ("Nothing was changed:\n  NOT changed: M2 (Outlook HTTP 503: busy)\n"
                             "  Skipped: 1 message already had it.")

    async def test_moves_run_at_most_four_at_a_time(self, monkeypatch):
        import asyncio

        flight = {"now": 0, "peak": 0}

        async def graph(request):
            path = request.url.path.split("/v1.0", 1)[-1]
            if request.method == "GET":  # the Archive folder
                return httpx.Response(200, json=_ARCHIVE_OK[1])
            flight["now"] += 1
            flight["peak"] = max(flight["peak"], flight["now"])
            await asyncio.sleep(0.01)
            flight["now"] -= 1
            return httpx.Response(201, json={"id": "N" + path.split("/")[3]})
        tool = _mail(_Graph({}), monkeypatch)
        tool._client = _RealAsyncClient(transport=httpx.MockTransport(graph))
        res = await tool.execute("archive", message_ids=[f"M{i}" for i in range(30)])
        assert res.success, res.error
        assert "Archived 30 messages" in res.content
        assert 1 < flight["peak"] <= 4

    # ── limits and the rest ──

    async def test_a_conversation_over_500_messages_is_refused(self, monkeypatch):
        huge = (200, {"value": [{"id": f"M{i}", "parentFolderId": "INBOX", "categories": []}
                                for i in range(501)]})
        graph = _Graph({**_KNOWN, _ARCHIVE: _ARCHIVE_OK, ("GET", "/me/messages"): huge})
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("archive", thread_ids=["C1"])
        assert not res.success
        assert res.error == ("These conversations hold 501 messages to move — at most 500 per call. "
                             "Pass fewer thread_ids. Nothing was moved.")
        res = await tool.execute("add_categories", thread_ids=["C1"], categories=["X"])
        assert not res.success
        assert res.error == ("These conversations hold 501 messages — at most 500 per call. "
                             "Pass fewer thread_ids. Nothing was changed.")
        assert {r.method for r in graph.requests} == {"GET"}

    async def test_well_known_folders_are_read_once_per_token(self, monkeypatch):
        graph = _Graph({_ARCHIVE: _ARCHIVE_OK, ("POST", "/me/messages/M1/move"): (201, {"id": "N1"})})
        tool = _mail(graph, monkeypatch)
        for _ in range(2):
            assert (await tool.execute("archive", message_ids=["M1"])).success
        assert sum(r.url.path == "/v1.0/me/mailFolders/archive" for r in graph.requests) == 1

    async def test_remove_categories_on_a_conversation(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/messages"): (200, {"value": [
                {"id": "M1", "categories": ["Finance", "x"]}, {"id": "M2", "categories": []}]}),
            ("PATCH", "/me/messages/M1"): (200, {}),
        })
        res = await _mail(graph, monkeypatch).execute(
            "remove_categories", thread_ids=["C1"], categories=["finance"])
        assert res.success, res.error
        assert graph.bodies("PATCH") == [{"categories": ["x"]}]
        assert "Removed category 'finance' from 1 message." in res.content
        assert "1 message didn't have it — unchanged." in res.content
        assert 'Undo: microsoft_mail action=add_categories categories=["Finance"] message_ids=[M1]' in res.content

    def test_category_names_from_a_python_style_list(self):
        from captain_claw.tools.microsoft_mail import _names

        assert _names("['Receipts', 'Travel']") == ["Receipts", "Travel"]
        assert _names("[Receipts, \"Red category\"]") == ["Receipts", "Red category"]
        assert _names('["Receipts", "Travel"]') == ["Receipts", "Travel"]
        assert _names("Receipts, travel, RECEIPTS") == ["Receipts", "travel"]
        assert _names("[EXT] Vendor") == ["[EXT] Vendor"]
        assert _names(["Students'"]) == ["Students'"]

    async def test_add_categories_from_a_python_style_list(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/messages/M1"): (200, {"id": "M1", "categories": []}),
            ("PATCH", "/me/messages/M1"): (200, {}),
        })
        res = await _mail(graph, monkeypatch).execute(
            "add_categories", message_ids=["M1"], categories="['Receipts', 'Travel']")
        assert res.success, res.error
        assert graph.bodies("PATCH") == [{"categories": ["Receipts", "Travel"]}]

    def test_bracketed_category_names_keep_their_brackets(self):
        from captain_claw.tools.microsoft_mail import _names

        # Not one list as a whole: comma-separated names, brackets kept.
        assert _names("[EXT]") == ["[EXT]"]
        assert _names("[A], [B]") == ["[A]", "[B]"]
        assert _names("[EXT], Travel") == ["[EXT]", "Travel"]
        # A quoted name loses only its own quotes.
        assert _names("'Receipts', \"Red category\"") == ["Receipts", "Red category"]
        assert _names("O'Brien, Students'") == ["O'Brien", "Students'"]
        # A list literal's names keep theirs.
        assert _names("['[EXT]', 'Travel']") == ["[EXT]", "Travel"]
        assert _names('["[A]", "[B]"]') == ["[A]", "[B]"]

    async def test_bracketed_categories_are_added_and_removed_as_named(self, monkeypatch):
        graph = _Graph({
            ("GET", "/me/messages/M1"): (200, {"id": "M1", "categories": []}),
            ("PATCH", "/me/messages/M1"): (200, {}),
            ("GET", "/me/messages/M2"): (200, {"id": "M2", "categories": ["[EXT]", "Blue"]}),
            ("PATCH", "/me/messages/M2"): (200, {}),
        })
        tool = _mail(graph, monkeypatch)
        res = await tool.execute("add_categories", message_ids=["M1"], categories="[A], [B]")
        assert res.success, res.error
        res = await tool.execute("remove_categories", message_ids=["M2"], categories="[EXT]")
        assert res.success, res.error
        assert "Removed category '[EXT]' from 1 message." in res.content
        assert graph.bodies("PATCH") == [{"categories": ["[A]", "[B]"]}, {"categories": ["Blue"]}]


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
