"""The agentic signature (Connections → Google / Microsoft) at the end of every
draft the agents' mail tools write — :mod:`captain_claw.mail_signature`, and
google_mail / microsoft_mail reading it from Flight Deck's agent_status.

(Flight Deck's side — storing it, agent_status, the Gmail sends it composes —
is in tests/test_flight_deck/test_gmail_send_routes.py and
test_microsoft_oauth_routes.py.)

Gmail, Graph and Flight Deck answer through httpx.MockTransport.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
import time

import httpx
import pytest

from captain_claw import mail_signature as ms
from captain_claw.google_oauth import GoogleOAuthTokens
from captain_claw.google_oauth_manager import GoogleOAuthManager

_RealAsyncClient = httpx.AsyncClient

SIG = "Sent by Ana's assistant\nAna & Co."


# ── the helper ───────────────────────────────────────────────────────


class TestHelper:
    def test_normalize(self):
        assert ms.normalize(None) == ""
        assert ms.normalize("  \r\nAna  \r\n\r\nCo.\x00\x07 \n\n") == "Ana\n\nCo."
        assert ms.normalize("x" * ms.MAX_CHARS) == "x" * ms.MAX_CHARS
        for bad in (3, ["a"], "x" * (ms.MAX_CHARS + 1)):
            with pytest.raises(ValueError):
                ms.normalize(bad)

    def test_plain_body_gets_it_after_a_blank_line(self):
        assert ms.apply("Hi Bob\n\n", "", SIG) == (f"Hi Bob\n\n{SIG}", "")

    def test_html_body_gets_an_escaped_block_inside_body(self):
        body, html = ms.apply("", "<html><body><p>Hi</p></body></html>", SIG)
        assert body == ""
        assert html == ("<html><body><p>Hi</p><div><br></div>"
                        "<div>Sent by Ana&#x27;s assistant<br>Ana &amp; Co.</div></body></html>")

    def test_both_parts_get_it(self):
        body, html = ms.apply("Hi", "<p>Hi</p>", SIG)
        assert body.endswith(SIG) and html.endswith("Ana &amp; Co.</div>")

    def test_an_empty_draft_is_just_the_signature(self):
        assert ms.apply("", "", SIG) == (SIG, "")
        assert ms.apply("  ", "", SIG) == (SIG, "")

    def test_never_twice(self):
        body, html = ms.apply("Hi", "<p>Hi</p>", SIG)
        assert ms.apply(body, html, SIG) == (body, html)
        # Reflowed whitespace still counts as already signed.
        assert ms.append_text("Hi\n\nSent by Ana's assistant  Ana & Co.\n", SIG) == \
            "Hi\n\nSent by Ana's assistant  Ana & Co.\n"

    def test_no_signature_changes_nothing(self):
        assert ms.apply("Hi", "<p>Hi</p>", "") == ("Hi", "<p>Hi</p>")
        assert ms.apply("", "", "") == ("", "")


# ── google_mail ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    import captain_claw.config as config_mod
    from captain_claw import mail_authority
    from captain_claw.config import Config
    from captain_claw.flight_deck import agent_secret

    for name in ("CLAW_BEING_WORKER", "CLAW_BASNA_WORKER", "CLAW_VATRA_WORKER",
                 "CLAW_COUNCIL_WORKER", "CLAW_CODE_AGENT", "FD_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FD_AGENT_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    agent_secret.reset_cache_for_tests()
    monkeypatch.setattr(config_mod, "_config", Config(web={"auth_token": "agent-web-auth"}))
    assert mail_authority.current() == mail_authority.HUMAN
    import captain_claw.microsoft_oauth_manager as mm
    mm._TOKENS = None
    yield
    mm._TOKENS = None
    agent_secret.reset_cache_for_tests()


def _flight_deck(monkeypatch, *, status: int = 200, answer: dict | None = None, base="http://fd.test"):
    """Flight Deck at *base* ("" = standalone) answering agent_status; returns
    the requests it got."""
    monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: base))
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=answer if answer is not None else {})

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler), **kw))
    return seen


def _google_token(monkeypatch):
    async def get_tokens(self):
        return GoogleOAuthTokens(
            access_token="T", refresh_token="", expires_at=time.time() + 3300,
            scope="https://www.googleapis.com/auth/gmail.readonly "
                  "https://www.googleapis.com/auth/gmail.compose")

    monkeypatch.setattr(GoogleOAuthManager, "get_tokens", get_tokens)


class _Gmail:
    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path.split("/gmail/v1/users/me", 1)[-1]
        status, body = self.routes.get((request.method, path), (404, {"error": {"message": "nope"}}))
        return httpx.Response(status, json=body)

    def message(self, method):
        (req,) = [r for r in self.requests if r.method == method]
        raw = json.loads(req.content)["message"]["raw"]
        return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                                        policy=email.policy.default)


def _gmail_tool(gmail):
    from captain_claw.tools.google_mail import GoogleMailTool

    tool = GoogleMailTool()
    tool._client = _RealAsyncClient(transport=httpx.MockTransport(gmail))
    return tool


def _parts(msg):
    plain = msg.get_body(("plain",)).get_content().replace("\r\n", "\n").rstrip()
    html = msg.get_body(("html",)).get_content()
    return plain, html


class TestGoogleMail:
    async def test_create_draft_ends_with_the_users_signature(self, monkeypatch):
        seen = _flight_deck(monkeypatch, answer={"connected": True, "mail_signature": SIG})
        _google_token(monkeypatch)
        gmail = _Gmail({("POST", "/drafts"): (200, {"id": "d1", "message": {"id": "m1"}})})
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="bob@x.co", subject="Hi", body="Hello Bob", allow_repeat=True)
        assert res.success, res.error
        assert "Signature: added at the end" in res.content
        plain, html = _parts(gmail.message("POST"))
        assert plain == f"Hello Bob\n\n{SIG}"
        assert "Sent by Ana&#x27;s assistant<br>Ana &amp; Co." in html
        (status,) = seen
        assert status.url.path == "/fd/google/agent_status"
        assert status.headers["X-Agent-Auth"] == "agent-web-auth"

    async def test_no_signature_set_leaves_the_draft_alone(self, monkeypatch):
        _flight_deck(monkeypatch, answer={"connected": True})
        _google_token(monkeypatch)
        gmail = _Gmail({("POST", "/drafts"): (200, {"id": "d1", "message": {"id": "m1"}})})
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="bob@x.co", subject="Hi", body="Hello Bob", allow_repeat=True)
        assert res.success and "Signature" not in res.content
        assert _parts(gmail.message("POST"))[0] == "Hello Bob"

    async def test_flight_deck_not_answering_says_so(self, monkeypatch):
        _flight_deck(monkeypatch, status=500)
        _google_token(monkeypatch)
        gmail = _Gmail({("POST", "/drafts"): (200, {"id": "d1", "message": {"id": "m1"}})})
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="bob@x.co", subject="Hi", body="Hello Bob", allow_repeat=True)
        assert res.success and "Signature: NOT added" in res.content
        assert _parts(gmail.message("POST"))[0] == "Hello Bob"

    async def test_standalone_never_asks(self, monkeypatch):
        seen = _flight_deck(monkeypatch, base="")
        _google_token(monkeypatch)
        gmail = _Gmail({("POST", "/drafts"): (200, {"id": "d1", "message": {"id": "m1"}})})
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="bob@x.co", subject="Hi", body="Hello Bob", allow_repeat=True)
        assert res.success and "Signature" not in res.content and seen == []

    async def test_update_draft_new_text_is_signed(self, monkeypatch):
        _flight_deck(monkeypatch, answer={"mail_signature": SIG})
        _google_token(monkeypatch)
        raw = base64.urlsafe_b64encode(
            b"To: bob@x.co\r\nSubject: Hi\r\nContent-Type: text/plain\r\n\r\nOld text\r\n",
        ).decode().rstrip("=")
        gmail = _Gmail({
            ("GET", "/drafts/d1"): (200, {"id": "d1", "message": {"id": "m1", "raw": raw}}),
            ("PUT", "/drafts/d1"): (200, {"id": "d1", "message": {"id": "m2"}}),
        })
        res = await _gmail_tool(gmail).execute("update_draft", draft_id="d1", body="New text")
        assert res.success, res.error
        assert "Signature: added at the end" in res.content
        assert _parts(gmail.message("PUT"))[0] == f"New text\n\n{SIG}"


# ── microsoft_mail ───────────────────────────────────────────────────


class _Graph:
    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path.split("/v1.0", 1)[-1]
        status, body = self.routes.get((request.method, path), (404, {"error": {"code": "x"}}))
        return httpx.Response(status, json=body)

    def bodies(self, method):
        return [json.loads(r.content or b"{}") for r in self.requests if r.method == method]


def _outlook(graph, monkeypatch):
    import captain_claw.tools.microsoft_mail as mod

    tool = mod.MicrosoftMailTool()
    tool._client = _RealAsyncClient(transport=httpx.MockTransport(graph))

    async def token(need, what):
        return "T", frozenset({"mail.readwrite"})

    monkeypatch.setattr(mod, "graph_token", token)
    return tool


class TestMicrosoftMail:
    async def test_create_draft_ends_with_the_owners_signature(self, monkeypatch):
        seen = _flight_deck(monkeypatch, answer={"connected": True, "mail_signature": SIG})
        graph = _Graph({("POST", "/me/messages"): (201, {"id": "D1", "subject": "Hi"})})
        res = await _outlook(graph, monkeypatch).execute(
            "create_draft", to="ana@x.co", subject="Hi", body="Hello", allow_repeat=True)
        assert res.success, res.error
        assert "Signature: added at the end" in res.content
        assert graph.bodies("POST")[0]["body"] == {"contentType": "text", "content": f"Hello\n\n{SIG}"}
        (status,) = seen
        assert status.url.path == "/fd/microsoft/agent_status"
        assert "X-FD-Speaker-Grant" not in status.headers

    async def test_html_draft_gets_the_html_block(self, monkeypatch):
        _flight_deck(monkeypatch, answer={"mail_signature": SIG})
        graph = _Graph({("POST", "/me/messages"): (201, {"id": "D1"})})
        res = await _outlook(graph, monkeypatch).execute(
            "create_draft", to="ana@x.co", subject="Hi", html_body="<p>Hello</p>", allow_repeat=True)
        assert res.success, res.error
        body = graph.bodies("POST")[0]["body"]
        assert body["contentType"] == "html" and body["content"].endswith("Ana &amp; Co.</div>")

    async def test_reply_text_ends_with_it(self, monkeypatch):
        _flight_deck(monkeypatch, answer={"mail_signature": SIG})
        graph = _Graph({
            ("GET", "/me/messages/M1"): (200, {
                "id": "M1", "subject": "Plan", "conversationId": "C1",
                "from": {"emailAddress": {"address": "ana@x.co"}}}),
            ("GET", "/me"): (200, {"mail": "me@contoso.com"}),
            ("POST", "/me/messages/M1/createReply"): (201, {"id": "R1"}),
        })
        res = await _outlook(graph, monkeypatch).execute(
            "create_draft", reply_to_message_id="M1", body="Sounds good", allow_repeat=True)
        assert res.success, res.error
        assert graph.bodies("POST")[0]["comment"] == f"Sounds good\n\n{SIG}"

    async def test_update_draft_new_text_is_signed(self, monkeypatch):
        seen = _flight_deck(monkeypatch, answer={"mail_signature": SIG})
        graph = _Graph({
            ("GET", "/me/messages/D1"): (200, {"id": "D1", "isDraft": True, "subject": "Hi"}),
            ("PATCH", "/me/messages/D1"): (200, {"id": "D1", "subject": "Hi"}),
        })
        res = await _outlook(graph, monkeypatch).execute("update_draft", draft_id="D1", body="New")
        assert res.success, res.error
        assert graph.bodies("PATCH")[0]["body"] == {"contentType": "text", "content": f"New\n\n{SIG}"}
        assert len(seen) == 1
