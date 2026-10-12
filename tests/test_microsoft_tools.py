"""microsoft_mail / microsoft_calendar / microsoft_onedrive and the agent side
of the Microsoft connection.

* MicrosoftOAuthManager: Flight Deck only, the owner's token, a member's turn
  refused before any request, FD's refusal reason kept;
* the registry hides ``requires_microsoft`` tools until connected;
* mail writes go through mail_authority like google_mail's (automated turns
  refused before any token fetch), drafts never send, repeats are refused;
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
