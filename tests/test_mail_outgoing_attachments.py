"""Attaching local files to outgoing email: google_mail (create_draft /
update_draft / send, standalone and through Flight Deck) and microsoft_mail
(create_draft / update_draft), plus the shared loader in mail_attachments.

Every file lives in tmp_path; Gmail, Graph and Flight Deck answer through
httpx.MockTransport — nothing here reaches Google, Microsoft or ~/.captain-claw.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
import time
from pathlib import Path

import httpx
import pytest

import captain_claw.session as session_mod
from captain_claw import gmail_compose, mail_attachments, speaker
from captain_claw.google_oauth import GoogleOAuthTokens
from captain_claw.google_oauth_manager import GoogleOAuthManager
from captain_claw.mail_attachments import Attachment, AttachmentError
from captain_claw.tools.google_mail import GoogleMailTool

_G = "https://www.googleapis.com/auth/"
SCOPES = f"{_G}gmail.readonly {_G}gmail.compose"
_RealAsyncClient = httpx.AsyncClient
PDF = b"%PDF-1.4 quarterly report"
CSV = b"region,total\nnorth,12\n"


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    import captain_claw.config as config_mod
    from captain_claw import mail_authority
    from captain_claw.config import Config
    from captain_claw.flight_deck import agent_secret

    monkeypatch.setattr(session_mod, "get_session_manager", lambda: object())
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    for name in ("CLAW_BEING_WORKER", "CLAW_BASNA_WORKER", "CLAW_VATRA_WORKER",
                 "CLAW_COUNCIL_WORKER", "CLAW_CODE_AGENT"):
        monkeypatch.delenv(name, raising=False)
    agent_secret.reset_cache_for_tests()
    cfg = Config()
    monkeypatch.setattr(config_mod, "_config", cfg)
    assert mail_authority.current() == mail_authority.HUMAN
    yield cfg
    agent_secret.reset_cache_for_tests()


@pytest.fixture()
def files(tmp_path) -> dict[str, Path]:
    saved = tmp_path / "saved" / "showcase" / "s1"
    saved.mkdir(parents=True)
    (saved / "report.pdf").write_bytes(PDF)
    (saved / "data.csv").write_bytes(CSV)
    return {"report": saved / "report.pdf", "csv": saved / "data.csv"}


def _rt(tmp_path) -> dict:
    return {"_runtime_base_path": tmp_path, "_saved_base_path": tmp_path / "saved"}


def _local_mode(monkeypatch) -> dict:
    calls = {"get_tokens": 0}
    monkeypatch.setattr(GoogleOAuthManager, "_flight_deck_base", staticmethod(lambda: ""))

    async def _get_tokens(self):
        calls["get_tokens"] += 1
        return GoogleOAuthTokens(access_token="T", refresh_token="", expires_at=time.time() + 3300,
                                 scope=SCOPES)

    monkeypatch.setattr(GoogleOAuthManager, "get_tokens", _get_tokens)
    return calls


class _Gmail:
    """Scripted Gmail incl. the resumable upload: opening an upload answers a
    session URL (``("START", method, path)`` overrides it), the PUT to it
    answers ``("UPLOAD", path)``; plain calls answer ``(method, path)``."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("/gmail/v1/users/me", 1)[-1]
        params = request.url.params
        if params.get("uploadType") == "resumable" and "upload_id" not in params:
            hit = self.routes.get(("START", request.method, path))
            if hit:
                status, body, headers = (*hit, {})[:3]
                return httpx.Response(status, json=body, headers=headers)
            return httpx.Response(200, headers={"Location": (
                f"https://gmail.googleapis.com/upload/gmail/v1/users/me{path}"
                "?uploadType=resumable&upload_id=U1")})
        if "upload_id" in params:
            status, body = self.routes.get(("UPLOAD", path), (404, {"error": {"message": "nope"}}))
            return httpx.Response(status, json=body)
        status, body = self.routes.get((request.method, path), (404, {"error": {"message": "nope"}}))
        return httpx.Response(status, json=body)

    def starts(self) -> list[httpx.Request]:
        return [r for r in self.requests
                if r.url.params.get("uploadType") == "resumable" and "upload_id" not in r.url.params]

    def uploads(self) -> list[httpx.Request]:
        return [r for r in self.requests if "upload_id" in r.url.params]

    def uploaded(self) -> email.message.EmailMessage:
        (put,) = self.uploads()
        return email.message_from_bytes(put.content, policy=email.policy.default)


def _gmail_tool(gmail: _Gmail) -> GoogleMailTool:
    tool = GoogleMailTool()
    tool._client = _RealAsyncClient(transport=httpx.MockTransport(gmail))
    return tool


def _files_of(msg) -> dict[str, bytes]:
    return {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}


# ── mail_attachments ─────────────────────────────────────────────────


class TestLoader:
    def test_parse_list(self):
        assert mail_attachments.parse_list(None) == []
        assert mail_attachments.parse_list("") == []
        assert mail_attachments.parse_list("a, b.pdf") == ["a, b.pdf"]  # one path
        assert mail_attachments.parse_list([" a.pdf ", "", "b.csv"]) == ["a.pdf", "b.csv"]
        with pytest.raises(AttachmentError):
            mail_attachments.parse_list({"x": 1})
        with pytest.raises(AttachmentError):
            mail_attachments.parse_list([1])

    async def test_resolves_like_the_read_tool(self, tmp_path, files):
        rt = _rt(tmp_path)
        for value in ("saved/showcase/s1/report.pdf", "showcase/s1/report.pdf", str(files["report"])):
            (att,) = await mail_attachments.load([value], rt, max_total_bytes=10**6, limit_label="x")
            assert att == Attachment("report.pdf", "application/pdf", PDF), value

    async def test_file_registry_fallback(self, tmp_path, files):
        class _Reg:
            def resolve(self, value):
                return str(files["csv"]) if value == "the-data" else None

        (att,) = await mail_attachments.load(
            ["the-data"], {**_rt(tmp_path), "_file_registry": _Reg()},
            max_total_bytes=10**6, limit_label="x")
        assert att.filename == "data.csv" and att.mime_type == "text/csv" and att.data == CSV

    async def test_missing_empty_too_many_too_big(self, tmp_path, files):
        rt = _rt(tmp_path)
        with pytest.raises(AttachmentError, match="Attachment not found: nope.pdf"):
            await mail_attachments.load(["nope.pdf"], rt, max_total_bytes=10**6, limit_label="x")
        (tmp_path / "empty.txt").write_bytes(b"")
        with pytest.raises(AttachmentError, match="is empty"):
            await mail_attachments.load(["empty.txt"], rt, max_total_bytes=10**6, limit_label="x")
        with pytest.raises(AttachmentError, match="Too many attachments"):
            await mail_attachments.load(["report.pdf"] * 21, rt, max_total_bytes=10**6, limit_label="x")
        with pytest.raises(AttachmentError, match="too large together.*the limit"):
            await mail_attachments.load([str(files["report"]), str(files["csv"])], rt,
                                        max_total_bytes=len(PDF) + 3, limit_label="the limit")

    @pytest.mark.parametrize("rel", [".ssh/id_rsa", "proj/.env", "proj/.env.local", "certs/server.pem",
                                     "x/.aws/credentials", "home/.netrc"])
    async def test_credentials_never_attach(self, tmp_path, rel):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret")
        with pytest.raises(AttachmentError, match="can't be attached"):
            await mail_attachments.load([str(target)], _rt(tmp_path), max_total_bytes=10**6, limit_label="x")

    async def test_a_keynote_deck_is_not_a_key(self, tmp_path):
        (tmp_path / "deck.key").write_bytes(b"keynote")
        (att,) = await mail_attachments.load([str(tmp_path / "deck.key")], _rt(tmp_path),
                                             max_total_bytes=10**6, limit_label="x")
        assert att.filename == "deck.key"

    async def test_member_only_attaches_their_own_files(self, tmp_path, files, monkeypatch):
        monkeypatch.setattr(speaker, "current", lambda: object())
        monkeypatch.setattr(speaker, "path_allowed", lambda p: False)
        with pytest.raises(AttachmentError, match=r"^Not allowed in a shared chat: "):
            await mail_attachments.load([str(files["report"])], _rt(tmp_path),
                                        max_total_bytes=10**6, limit_label="x")
        monkeypatch.setattr(speaker, "path_allowed", lambda p: True)
        assert await mail_attachments.load([str(files["report"])], _rt(tmp_path),
                                           max_total_bytes=10**6, limit_label="x")

    async def test_a_member_cant_slip_a_path_in_as_a_string(self, monkeypatch, tmp_path, files):
        # "/attachments/*" checks list items only; a bare string still meets path_allowed.
        monkeypatch.setattr(speaker, "current", lambda: object())
        monkeypatch.setattr(speaker, "path_allowed", lambda p: False)
        _local_mode(monkeypatch)
        gmail = _Gmail()
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="a@b.co", body="x", attachments=str(files["report"]), **_rt(tmp_path))
        assert not res.success and res.error.startswith("Not allowed in a shared chat: ")
        assert gmail.requests == []
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="a@b.co", body="x", attachments=["/no/such/dir/x.pdf"], **_rt(tmp_path))
        assert "Attachment not found: x.pdf." in res.error and "/no/such" not in res.error

    async def test_databases_and_data_folders_never_attach(self, tmp_path, monkeypatch):
        db = tmp_path / "saved" / "export.sqlite"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
        with pytest.raises(AttachmentError, match="is a database"):
            await mail_attachments.load([str(db)], _rt(tmp_path), max_total_bytes=10**6, limit_label="x")
        fd_data = tmp_path / "fd-data"
        (fd_data / "secrets").mkdir(parents=True)
        (fd_data / "secrets" / "config.yaml").write_text("api_key: x")
        monkeypatch.setenv("FD_DATA_DIR", str(fd_data))
        with pytest.raises(AttachmentError, match="own data folder"):
            await mail_attachments.load([str(fd_data / "secrets" / "config.yaml")], _rt(tmp_path),
                                        max_total_bytes=10**6, limit_label="x")
        # The saved/ area inside a data folder stays attachable.
        saved = fd_data / "agent" / "saved"
        saved.mkdir(parents=True)
        (saved / "r.pdf").write_bytes(PDF)
        rt = {"_runtime_base_path": fd_data / "agent", "_saved_base_path": saved}
        assert await mail_attachments.load([str(saved / "r.pdf")], rt, max_total_bytes=10**6, limit_label="x")

    def test_names_and_types(self):
        assert mail_attachments.safe_filename("../../etc/pass\nwd") == "pass wd"
        assert mail_attachments.safe_filename("a‮gnp.exe") == "a gnp.exe"
        long = "x" * 300 + ".pdf"
        cut = mail_attachments.safe_filename(long)
        assert cut.endswith(".pdf") and len(cut.encode()) <= 180
        assert mail_attachments.safe_filename("  ") == "attachment"
        assert mail_attachments.guess_mime("fwd.eml") == "application/octet-stream"
        assert mail_attachments.guess_mime("notes.md") == "text/markdown"
        assert mail_attachments.guess_mime("x.unknownext") == "application/octet-stream"
        assert mail_attachments.clean_mime("message/rfc822", "a.eml") == "application/octet-stream"
        assert mail_attachments.clean_mime("bad type", "a.pdf") == "application/pdf"
        names = [a.filename for a in mail_attachments.unique_names(
            [Attachment("a.pdf", "application/pdf", b"1"), Attachment("A.pdf", "application/pdf", b"2"),
             Attachment("noext", "text/plain", b"3"), Attachment("noext", "text/plain", b"4")])]
        assert names == ["a.pdf", "A (2).pdf", "noext", "noext (2)"]

    def test_wire_round_trip_and_checks(self):
        atts = [Attachment("r.pdf", "application/pdf", PDF)]
        assert mail_attachments.from_wire(mail_attachments.to_wire(atts), max_total_bytes=10**6) == atts
        assert mail_attachments.from_wire(None, max_total_bytes=1) == []
        bad = [
            ("nope", "must be a list"),
            ([{"filename": "a"}], "has no data"),
            ([{"filename": "a", "data": "@@@"}], "not valid base64"),
            ([{"filename": "a", "data": "QUJD"}] * 21, "Too many"),
            ([{"filename": "a", "data": base64.b64encode(b"x" * 50).decode()}], "too large"),
        ]
        for raw, needle in bad:
            with pytest.raises(AttachmentError, match=needle):
                mail_attachments.from_wire(raw, max_total_bytes=40)
        (a,) = mail_attachments.from_wire(
            [{"filename": "../x\r\n.pdf", "mime_type": "multipart/mixed", "data": "QUJD"}],
            max_total_bytes=40)
        assert a.filename == "x .pdf" and a.mime_type == "application/octet-stream" and a.data == b"ABC"

    def test_message_carries_the_files_and_the_body(self):
        raw = gmail_compose.build_message_bytes(
            to="bob@x.co", subject="Report", body="See attached.",
            attachments=[Attachment("Poročilo Q3.pdf", "application/pdf", PDF),
                         Attachment("data.csv", "text/csv", CSV)])
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        assert msg.get_content_type() == "multipart/mixed"
        assert msg.get_body(("plain",)).get_content().strip() == "See attached."
        assert _files_of(msg) == {"Poročilo Q3.pdf": PDF, "data.csv": CSV}
        # No files: the same text + HTML alternative as before.
        raw = gmail_compose.build_raw_message(to="a@b.co", subject="s", body="b")
        plain = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                                         policy=email.policy.default)
        assert plain.get_content_type() == "multipart/alternative"

    def test_content_hash_counts_the_files(self):
        base = gmail_compose.content_hash("a@b.co", "", "", "s", "b")
        assert gmail_compose.content_hash("a@b.co", "", "", "s", "b", attachments_digest="") == base
        one = mail_attachments.digest([Attachment("r.pdf", "application/pdf", PDF)])
        two = mail_attachments.digest([Attachment("r.pdf", "application/pdf", PDF + b"!")])
        assert one != two
        assert gmail_compose.content_hash("a@b.co", "", "", "s", "b", attachments_digest=one) != base


# ── google_mail ──────────────────────────────────────────────────────


class TestGmailDrafts:
    async def test_create_draft_uploads_the_files(self, monkeypatch, tmp_path, files):
        _local_mode(monkeypatch)
        gmail = _Gmail({("UPLOAD", "/drafts"): (200, {"id": "d1", "message": {"id": "m1"}})})
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="bob@x.co", subject="Q3", body="Here it is.", allow_repeat=True,
            attachments=["saved/showcase/s1/report.pdf", str(files["csv"])], **_rt(tmp_path))

        assert res.success, res.error
        assert "Draft ID: d1" in res.content
        assert "Attachments: report.pdf (25 B), data.csv (22 B)" in res.content
        assert "Path:" not in res.content and str(tmp_path) not in res.content
        (start,) = gmail.starts()
        assert start.method == "POST" and start.url.path == "/upload/gmail/v1/users/me/drafts"
        assert start.headers["Authorization"] == "Bearer T"
        assert start.headers["X-Upload-Content-Type"] == "message/rfc822"
        assert json.loads(start.content) == {"message": {}}
        put = gmail.uploads()[0]
        assert put.method == "PUT" and put.headers["Content-Type"] == "message/rfc822"
        assert start.headers["X-Upload-Content-Length"] == str(len(put.content))
        msg = gmail.uploaded()
        assert msg["To"] == "bob@x.co" and msg["Subject"] == "Q3"
        assert _files_of(msg) == {"report.pdf": PDF, "data.csv": CSV}
        # Never the JSON raw form for a message with files.
        assert not [r for r in gmail.requests if r.url.path == "/gmail/v1/users/me/drafts"]

    async def test_reply_draft_with_files_stays_in_the_thread(self, monkeypatch, tmp_path, files):
        _local_mode(monkeypatch)
        gmail = _Gmail({
            ("GET", "/messages/m1"): (200, {"id": "m1", "threadId": "t1", "payload": {"headers": [
                {"name": "From", "value": "Ana <ana@ok.com>"}, {"name": "Subject", "value": "Plan"},
                {"name": "Message-ID", "value": "<orig@ok.com>"}]}}),
            ("UPLOAD", "/drafts"): (200, {"id": "d2", "message": {"id": "m2"}}),
        })
        res = await _gmail_tool(gmail).execute(
            "create_draft", reply_to_message_id="m1", body="Attached.", allow_repeat=True,
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert res.success, res.error
        assert json.loads(gmail.starts()[0].content) == {"message": {"threadId": "t1"}}
        msg = gmail.uploaded()
        assert msg["In-Reply-To"] == "<orig@ok.com>" and msg["Subject"] == "Re: Plan"

    async def test_unusable_file_refuses_before_any_request(self, monkeypatch, tmp_path):
        calls = _local_mode(monkeypatch)
        (tmp_path / ".ssh").mkdir()
        (tmp_path / ".ssh" / "id_ed25519").write_bytes(b"KEY")
        gmail = _Gmail()
        tool = _gmail_tool(gmail)
        res = await tool.execute("create_draft", to="bob@x.co", subject="s", body="b",
                                 attachments=["missing.pdf"], **_rt(tmp_path))
        assert not res.success and "Attachment not found: missing.pdf" in res.error
        assert res.error.endswith("No draft was created.")
        res = await tool.execute("send", to="bob@x.co", subject="s", body="b",
                                 attachments=[str(tmp_path / ".ssh" / "id_ed25519")], **_rt(tmp_path))
        assert not res.success and "can't be attached" in res.error and "Nothing was sent." in res.error
        assert gmail.requests == [] and calls["get_tokens"] == 0

    async def test_over_gmails_limit_is_refused(self, monkeypatch, tmp_path, files):
        _local_mode(monkeypatch)
        monkeypatch.setattr(mail_attachments, "GMAIL_MAX_MESSAGE_BYTES", 400)
        gmail = _Gmail()
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="bob@x.co", subject="s", body="b", allow_repeat=True,
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert not res.success and "at most 25 MB" in res.error and "No draft was created." in res.error
        assert gmail.starts() == []

    async def test_upload_session_must_be_googles(self, monkeypatch, tmp_path, files):
        _local_mode(monkeypatch)
        gmail = _Gmail({("START", "POST", "/drafts"): (200, {}, {"Location": "https://evil.example/u"})})
        res = await _gmail_tool(gmail).execute(
            "create_draft", to="bob@x.co", subject="s", body="b", allow_repeat=True,
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert not res.success and gmail.uploads() == []
        assert all("evil.example" not in str(r.url) for r in gmail.requests)

    async def test_send_draft_and_create_draft_refuse_what_they_cant_do(self, monkeypatch, tmp_path, files):
        calls = _local_mode(monkeypatch)
        gmail = _Gmail()
        tool = _gmail_tool(gmail)
        res = await tool.execute("send_draft", draft_id="d1", attachments=[str(files["report"])],
                                 **_rt(tmp_path))
        assert not res.success and "update_draft" in res.error and "Nothing was sent." in res.error
        res = await tool.execute("create_draft", to="b@x.co", body="b", remove_attachments=["1"],
                                 **_rt(tmp_path))
        assert not res.success and "only with update_draft" in res.error
        assert gmail.requests == [] and calls["get_tokens"] == 0


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


OLD_PDF = b"%PDF-1.4 old version"
NOTES = b"meeting notes"
LOGO = b"\x89PNG\r\n\x1a\nlogo"


def _raw_draft(*, inline: bool = False, text: str = "The numbers are attached.",
               charset: str = "utf-8") -> dict:
    """drafts.get?format=raw for a draft with text + HTML, two files and,
    with *inline*, a logo the HTML shows (multipart/related, Content-ID)."""
    from email.message import EmailMessage

    msg = EmailMessage(policy=email.policy.SMTP)
    msg["To"] = "Dave <dave@ok.com>"
    msg["Subject"] = "Numbers"
    msg.set_content(text, charset=charset, cte="8bit")
    msg.add_alternative('<div>The numbers.<img src="cid:logo1"></div>', subtype="html")
    if inline:
        msg.get_payload()[1].add_related(LOGO, "image", "png", cid="<logo1>", filename="logo.png")
    msg.add_attachment(OLD_PDF, maintype="application", subtype="pdf", filename="old.pdf")
    msg.add_attachment(NOTES, maintype="text", subtype="plain", filename="notes.txt")
    return {"id": "d1", "message": {"id": "m9", "threadId": "td", "raw": _b64(msg.as_bytes())}}


def _draft_gmail(draft: dict | None = None) -> _Gmail:
    return _Gmail({
        ("GET", "/drafts/d1"): (200, draft or _raw_draft()),
        ("UPLOAD", "/drafts/d1"): (200, {"id": "d1", "message": {"id": "m10"}}),
        ("PUT", "/drafts/d1"): (200, {"id": "d1", "message": {"id": "m11"}}),
    })


def _plain(msg) -> str:
    return msg.get_body(("plain",)).get_content().strip()


class TestGmailUpdateDraft:
    async def test_adds_files_and_keeps_the_drafts_own(self, monkeypatch, tmp_path, files):
        _local_mode(monkeypatch)
        gmail = _draft_gmail()
        res = await _gmail_tool(gmail).execute(
            "update_draft", draft_id="d1", attachments=[str(files["csv"])], **_rt(tmp_path))

        assert res.success, res.error
        assert "Attachments: old.pdf (20 B), notes.txt (13 B), data.csv (22 B)" in res.content
        assert gmail.requests[0].url.params["format"] == "raw"
        start = gmail.starts()[0]
        assert start.method == "PUT" and start.url.path == "/upload/gmail/v1/users/me/drafts/d1"
        assert json.loads(start.content) == {"id": "d1", "message": {}}
        msg = gmail.uploaded()
        # Only files changed: the draft keeps its text, recipients and subject.
        assert _plain(msg) == "The numbers are attached."
        assert msg["To"] == "Dave <dave@ok.com>" and msg["Subject"] == "Numbers"
        assert _files_of(msg) == {"old.pdf": OLD_PDF, "notes.txt": NOTES, "data.csv": CSV}

    async def test_a_compose_only_grant_is_enough(self, monkeypatch, tmp_path, files):
        # attachments.get needs a read scope; the raw draft doesn't.
        _local_mode(monkeypatch)

        async def _compose_only(self):
            return GoogleOAuthTokens(access_token="T", refresh_token="", expires_at=time.time() + 3300,
                                     scope=f"{_G}gmail.compose")

        monkeypatch.setattr(GoogleOAuthManager, "get_tokens", _compose_only)
        gmail = _draft_gmail()
        res = await _gmail_tool(gmail).execute("update_draft", draft_id="d1", body="New text.",
                                               **_rt(tmp_path))
        assert res.success, res.error
        assert not [r for r in gmail.requests if "/attachments/" in r.url.path]

    async def test_a_body_change_keeps_the_files(self, monkeypatch, tmp_path):
        # update_draft used to drop every attachment (drafts.update replaces the message).
        _local_mode(monkeypatch)
        gmail = _draft_gmail()
        res = await _gmail_tool(gmail).execute("update_draft", draft_id="d1", body="New text.",
                                               subject="Numbers v2", **_rt(tmp_path))
        assert res.success, res.error
        msg = gmail.uploaded()
        assert _plain(msg) == "New text." and msg["Subject"] == "Numbers v2"
        assert _files_of(msg) == {"old.pdf": OLD_PDF, "notes.txt": NOTES}

    async def test_same_name_replaces_and_remove_by_number(self, monkeypatch, tmp_path):
        _local_mode(monkeypatch)
        new_pdf = tmp_path / "saved" / "old.pdf"
        new_pdf.parent.mkdir(parents=True, exist_ok=True)
        new_pdf.write_bytes(PDF)
        gmail = _draft_gmail()
        res = await _gmail_tool(gmail).execute(
            "update_draft", draft_id="d1", attachments=[str(new_pdf)], remove_attachments=["2"],
            **_rt(tmp_path))
        assert res.success, res.error
        assert _files_of(gmail.uploaded()) == {"old.pdf": PDF}
        assert "Attachments: old.pdf (25 B)" in res.content

    async def test_remove_all_keeps_the_text(self, monkeypatch, tmp_path):
        _local_mode(monkeypatch)
        gmail = _draft_gmail()
        res = await _gmail_tool(gmail).execute(
            "update_draft", draft_id="d1", remove_attachments=["all"], **_rt(tmp_path))
        assert res.success, res.error
        assert "Attachments: none" in res.content
        msg = gmail.uploaded()
        assert list(msg.iter_attachments()) == [] and _plain(msg) == "The numbers are attached."

    async def test_inline_images_stay_with_their_text(self, monkeypatch, tmp_path):
        _local_mode(monkeypatch)
        gmail = _draft_gmail(_raw_draft(inline=True))
        res = await _gmail_tool(gmail).execute(
            "update_draft", draft_id="d1", remove_attachments=["all"], **_rt(tmp_path))
        assert res.success, res.error
        assert "Attachments: logo.png (inline image, 12 B)" in res.content
        msg = gmail.uploaded()
        (logo,) = [p for p in msg.walk() if p.get_content_type() == "image/png"]
        assert logo["Content-ID"] == "<logo1>" and logo.get_payload(decode=True) == LOGO
        assert "cid:logo1" in msg.get_body(("html",)).get_content()

    async def test_new_text_drops_the_old_inline_images_and_says_so(self, monkeypatch, tmp_path):
        _local_mode(monkeypatch)
        gmail = _draft_gmail(_raw_draft(inline=True))
        res = await _gmail_tool(gmail).execute("update_draft", draft_id="d1", body="Plain now.",
                                               **_rt(tmp_path))
        assert res.success, res.error
        assert "1 inline image(s) of the old text were dropped" in res.content
        assert _files_of(gmail.uploaded()) == {"old.pdf": OLD_PDF, "notes.txt": NOTES}

    async def test_non_utf8_text_survives_a_files_only_update(self, monkeypatch, tmp_path, files):
        _local_mode(monkeypatch)
        gmail = _draft_gmail(_raw_draft(text="Številke so priložene, čestitke.", charset="windows-1250"))
        res = await _gmail_tool(gmail).execute(
            "update_draft", draft_id="d1", attachments=[str(files["csv"])], **_rt(tmp_path))
        assert res.success, res.error
        assert _plain(gmail.uploaded()) == "Številke so priložene, čestitke."

    async def test_unknown_name_or_no_content_changes_nothing(self, monkeypatch, tmp_path):
        _local_mode(monkeypatch)
        gmail = _draft_gmail()
        res = await _gmail_tool(gmail).execute(
            "update_draft", draft_id="d1", remove_attachments=["budget.xlsx"], **_rt(tmp_path))
        assert not res.success and "[1] old.pdf, [2] notes.txt" in res.error
        assert "Nothing was changed." in res.error and gmail.starts() == []
        gmail = _draft_gmail({"id": "d1", "message": {"id": "m9", "payload": {"headers": []}}})
        res = await _gmail_tool(gmail).execute("update_draft", draft_id="d1", remove_attachments=["1"],
                                               **_rt(tmp_path))
        assert not res.success and "pass the full body" in res.error
        assert not [r for r in gmail.requests if r.method == "PUT"]

    async def test_still_needs_a_body_or_a_file_change(self, monkeypatch, tmp_path):
        _local_mode(monkeypatch)
        gmail = _draft_gmail()
        res = await _gmail_tool(gmail).execute("update_draft", draft_id="d1", **_rt(tmp_path))
        assert not res.success and "pass the full new body" in res.error and gmail.requests == []


class TestGmailSend:
    async def test_local_send_uploads_with_the_thread(self, monkeypatch, tmp_path, files, _isolated):
        _isolated.tools.google_mail.allow_send = True
        _local_mode(monkeypatch)
        gmail = _Gmail({("UPLOAD", "/messages/send"): (200, {"id": "s1", "threadId": "t9"})})
        res = await _gmail_tool(gmail).execute(
            "send", to="bob@x.co", subject="Report", body="Attached.", allow_repeat=True,
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert res.success, res.error
        assert "Email sent." in res.content and "Message ID: s1" in res.content
        assert "Attachments: report.pdf (25 B)" in res.content
        start = gmail.starts()[0]
        assert start.url.path == "/upload/gmail/v1/users/me/messages/send"
        assert json.loads(start.content) == {}
        assert _files_of(gmail.uploaded()) == {"report.pdf": PDF}

    async def test_local_send_says_whether_it_may_have_gone(self, monkeypatch, tmp_path, files, _isolated):
        _isolated.tools.google_mail.allow_send = True
        _local_mode(monkeypatch)
        gmail = _Gmail({("START", "POST", "/messages/send"): (500, {"error": {"message": "x"}})})
        res = await _gmail_tool(gmail).execute(
            "send", to="bob@x.co", subject="R", body="b", allow_repeat=True,
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert not res.success and res.error.endswith("Nothing was sent.")
        gmail = _Gmail({("UPLOAD", "/messages/send"): (503, {"error": {"message": "x"}})})
        res = await _gmail_tool(gmail).execute(
            "send", to="bob@x.co", subject="R", body="b", allow_repeat=True,
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert not res.success and "may or may not have been sent" in res.error
        assert "in:sent" in res.error

    async def test_local_send_draft_lists_the_drafts_files(self, monkeypatch, tmp_path, _isolated):
        _isolated.tools.google_mail.allow_send = True
        _local_mode(monkeypatch)
        gmail = _Gmail({
            ("GET", "/drafts/d1"): (200, {"id": "d1", "message": {"id": "m9", "payload": {
                "headers": [{"name": "To", "value": "dave@ok.com"}, {"name": "Subject", "value": "N"}],
                "parts": [{"mimeType": "text/plain", "body": {"data": _b64(b"hi")}},
                          {"mimeType": "application/pdf", "filename": "old.pdf",
                           "body": {"attachmentId": "A1", "size": 20}}]}}}),
            ("POST", "/drafts/send"): (200, {"id": "s1", "threadId": "t1"}),
        })
        res = await _gmail_tool(gmail).execute("send_draft", draft_id="d1", **_rt(tmp_path))
        assert res.success, res.error
        assert "Email sent." in res.content and "Attachments: old.pdf (20 B)" in res.content


def _fd_mode(monkeypatch, *, takes_files: bool = True, sent: dict | None = None) -> list:
    """Flight Deck client mode; FD answers agent_status (with or without the
    attachments capability) and the send."""
    import captain_claw.config as config_mod
    from captain_claw.config import Config

    monkeypatch.setattr(config_mod, "_config", Config(
        web={"auth_token": "agent-web-auth"},
        google_oauth={"flight_deck_url": "http://fd.test", "flight_deck_secret": ""},
    ))
    monkeypatch.delenv("FD_AGENT_SHARED_SECRET", raising=False)
    seen: list[httpx.Request] = []
    answer = sent if sent is not None else {
        "ok": True, "message_id": "s7", "thread_id": "t7", "to": "bob@x.co", "cc": "",
        "bcc": "", "subject": "Hello", "attachments": [{"filename": "report.pdf", "size": 25}],
        "sent_last_24h": 1, "daily_limit": 50}

    def handler(request):
        seen.append(request)
        if request.url.path == "/fd/google/agent_status":
            status = {"connected": True, "enabled": True}
            if takes_files:
                status["gmail_send_attachments"] = True
            return httpx.Response(200, json=status)
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler), **kw))
    return seen


class TestGmailViaFlightDeck:
    async def test_files_go_to_flight_deck(self, monkeypatch, tmp_path, files):
        seen = _fd_mode(monkeypatch)
        res = await GoogleMailTool().execute(
            "send", to="bob@x.co", subject="Hello", body="Hi",
            attachments=["showcase/s1/report.pdf"], **_rt(tmp_path))
        assert res.success, res.error
        status, send = seen
        assert status.url.path == "/fd/google/agent_status"
        assert send.headers["X-Agent-Auth"] == status.headers["X-Agent-Auth"] == "agent-web-auth"
        assert str(send.url) == "http://fd.test/fd/google/gmail/send"
        body = json.loads(send.content)
        assert body["attachments"] == [{"filename": "report.pdf", "mime_type": "application/pdf",
                                        "data": base64.b64encode(PDF).decode()}]
        assert "Attachments: report.pdf (25 B)" in res.content and "WARNING" not in res.content

    async def test_an_older_flight_deck_is_never_asked_to_send_files(self, monkeypatch, tmp_path, files):
        seen = _fd_mode(monkeypatch, takes_files=False)
        res = await GoogleMailTool().execute(
            "send", to="bob@x.co", subject="Hello", body="Hi",
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert not res.success and "Nothing was sent." in res.error and "send_draft" in res.error
        assert [r.url.path for r in seen] == ["/fd/google/agent_status"]

    async def test_files_flight_deck_didnt_confirm_are_called_out(self, monkeypatch, tmp_path, files):
        _fd_mode(monkeypatch, sent={"ok": True, "message_id": "s7", "to": "bob@x.co", "subject": "Hello"})
        res = await GoogleMailTool().execute(
            "send", to="bob@x.co", subject="Hello", body="Hi",
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert res.success and "WARNING: Flight Deck did not confirm these files: report.pdf" in res.content

    async def test_no_files_no_capability_check(self, monkeypatch, tmp_path):
        seen = _fd_mode(monkeypatch, takes_files=False)
        res = await GoogleMailTool().execute("send", to="bob@x.co", subject="Hello", body="Hi",
                                             **_rt(tmp_path))
        assert res.success and [r.url.path for r in seen] == ["/fd/google/gmail/send"]


class TestMemberPathRule:
    def test_google_mail_attachments_are_a_read_path(self):
        rules = speaker.SPEAKER_PATH_MAP["google_mail"]
        assert speaker.PathRule("/attachments/*", "read_abs") in rules

    def test_a_member_without_file_roots_cant_attach(self):
        args, err = speaker.check_tool_paths(
            "google_mail", {"action": "send", "attachments": ["saved/x.pdf"]}, None)
        assert err == speaker.FILES_UNAVAILABLE_MESSAGE
        args, err = speaker.check_tool_paths("google_mail", {"action": "send", "to": "a@b.co"}, None)
        assert err is None


# ── microsoft_mail ───────────────────────────────────────────────────


class _Graph:
    """Graph routes keyed by (METHOD, path after /v1.0, or the full path for
    the upload session); a callable value gets the request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("/v1.0", 1)[-1]
        hit = self.routes.get((request.method, path))
        if callable(hit):
            hit = hit(request)
        status, body = hit if hit else (404, {"error": {"code": "itemNotFound", "message": "nope"}})
        return httpx.Response(status, json=body)

    def of(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(path)]


def _ms_tool(graph: _Graph, monkeypatch):
    import captain_claw.tools.microsoft_mail as mod

    tool = mod.MicrosoftMailTool()
    tool._client = _RealAsyncClient(transport=httpx.MockTransport(graph))
    calls = {"token": 0}

    async def token(need, what):
        calls["token"] += 1
        return "T", frozenset({"mail.readwrite"})

    monkeypatch.setattr(mod, "graph_token", token)
    tool._calls = calls
    return tool


_NEW_DRAFT = ("POST", "/me/messages")
_D1 = {"id": "D1", "subject": "Q3", "toRecipients": [{"emailAddress": {"address": "ana@x.co"}}]}


class TestOutlook:
    async def test_create_draft_attaches_a_small_file_in_one_post(self, monkeypatch, tmp_path, files):
        graph = _Graph({_NEW_DRAFT: (201, _D1),
                        ("POST", "/me/messages/D1/attachments"): (201, {"id": "AT1"})})
        res = await _ms_tool(graph, monkeypatch).execute(
            "create_draft", to="ana@x.co", subject="Q3", body="Attached.", allow_repeat=True,
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert res.success, res.error
        assert "Draft ID: D1" in res.content and "Attachments: report.pdf (25 B)" in res.content
        (post,) = graph.of("POST", "/me/messages/D1/attachments")
        assert json.loads(post.content) == {
            "@odata.type": "#microsoft.graph.fileAttachment", "name": "report.pdf",
            "contentType": "application/pdf", "contentBytes": base64.b64encode(PDF).decode()}

    async def test_a_large_file_goes_through_an_upload_session(self, monkeypatch, tmp_path, files):
        import captain_claw.tools.microsoft_mail as mod

        monkeypatch.setattr(mod, "_SMALL_ATTACHMENT_BYTES", 10)
        monkeypatch.setattr(mod, "_UPLOAD_CHUNK_BYTES", 8)
        session = "https://outlook.office.com/api/v2.0/AttachmentSessions('S1')?authtoken=x"
        graph = _Graph({
            _NEW_DRAFT: (201, _D1),
            ("POST", "/me/messages/D1/attachments/createUploadSession"): (201, {"uploadUrl": session}),
            ("PUT", "/api/v2.0/AttachmentSessions('S1')"): (200, {}),
        })
        res = await _ms_tool(graph, monkeypatch).execute(
            "create_draft", to="ana@x.co", subject="Q3", body="x", allow_repeat=True,
            attachments=[str(files["csv"])], **_rt(tmp_path))
        assert res.success, res.error
        assert "Attachments: data.csv (22 B)" in res.content
        (open_,) = graph.of("POST", "/createUploadSession")
        assert json.loads(open_.content) == {"AttachmentItem": {
            "attachmentType": "file", "name": "data.csv", "size": len(CSV), "contentType": "text/csv"}}
        puts = [r for r in graph.requests if r.method == "PUT"]
        assert [r.headers["Content-Range"] for r in puts] == [
            "bytes 0-7/22", "bytes 8-15/22", "bytes 16-21/22"]
        assert b"".join(r.content for r in puts) == CSV
        assert all("authorization" not in {k.lower() for k in r.headers} for r in puts)

    async def test_a_failed_file_is_named_and_the_draft_reported(self, monkeypatch, tmp_path, files):
        graph = _Graph({_NEW_DRAFT: (201, _D1),
                        ("POST", "/me/messages/D1/attachments"): (
                            413, {"error": {"code": "ErrorRequestTooLarge", "message": "too big"}})})
        res = await _ms_tool(graph, monkeypatch).execute(
            "create_draft", to="ana@x.co", subject="Q3", body="x", allow_repeat=True,
            attachments=[str(files["report"])], **_rt(tmp_path))
        assert res.success and "Draft ID: D1" in res.content
        assert "Attachments:" not in res.content
        assert "NOT attached: report.pdf (Outlook HTTP 413: too big)" in res.content
        assert "update_draft draft_id=D1" in res.content

    async def test_missing_file_refused_before_token_or_draft(self, monkeypatch, tmp_path):
        graph = _Graph({})
        tool = _ms_tool(graph, monkeypatch)
        res = await tool.execute("create_draft", to="ana@x.co", subject="s", body="b",
                                 attachments=["nope.pdf"], **_rt(tmp_path))
        assert not res.success and "No draft was created." in res.error
        assert graph.requests == [] and tool._calls["token"] == 0

    async def test_update_draft_files_only_adds_replaces_and_removes(self, monkeypatch, tmp_path):
        new_pdf = tmp_path / "old.pdf"
        new_pdf.write_bytes(PDF)
        listings = iter([
            [{"id": "A1", "name": "old.pdf", "size": 20}, {"id": "A2", "name": "notes.txt", "size": 13}],
            [{"id": "A3", "name": "old.pdf", "size": len(PDF)}],
        ])
        graph = _Graph({
            ("GET", "/me/messages/D1"): (200, {**_D1, "isDraft": True}),
            ("GET", "/me/messages/D1/attachments"): lambda r: (200, {"value": next(listings)}),
            ("POST", "/me/messages/D1/attachments"): (201, {"id": "A3"}),
            ("DELETE", "/me/messages/D1/attachments/A1"): (204, {}),
            ("DELETE", "/me/messages/D1/attachments/A2"): (204, {}),
        })
        res = await _ms_tool(graph, monkeypatch).execute(
            "update_draft", draft_id="D1", attachments=[str(new_pdf)], remove_attachments=["notes.txt"],
            **_rt(tmp_path))
        assert res.success, res.error
        assert "Attachments: old.pdf (25 B)" in res.content
        assert graph.of("PATCH", "/me/messages/D1") == []  # only files changed
        assert {r.url.path.rsplit("/", 1)[-1] for r in graph.requests if r.method == "DELETE"} == {"A1", "A2"}

    async def test_old_file_stays_when_its_replacement_failed(self, monkeypatch, tmp_path):
        new_pdf = tmp_path / "old.pdf"
        new_pdf.write_bytes(PDF)
        graph = _Graph({
            ("GET", "/me/messages/D1"): (200, {**_D1, "isDraft": True}),
            ("GET", "/me/messages/D1/attachments"): (200, {"value": [{"id": "A1", "name": "old.pdf", "size": 20}]}),
            ("POST", "/me/messages/D1/attachments"): (500, {"error": {"code": "x", "message": "boom"}}),
            ("PATCH", "/me/messages/D1"): (200, {**_D1, "isDraft": True}),
        })
        res = await _ms_tool(graph, monkeypatch).execute(
            "update_draft", draft_id="D1", body="New", attachments=[str(new_pdf)], **_rt(tmp_path))
        assert res.success and "NOT attached: old.pdf" in res.content
        assert [r for r in graph.requests if r.method == "DELETE"] == []
        assert graph.of("PATCH", "/me/messages/D1")  # the body change still went in

    async def test_all_keeps_inline_images(self, monkeypatch, tmp_path):
        listings = iter([
            [{"id": "A1", "name": "old.pdf", "size": 20},
             {"id": "L1", "name": "logo.png", "size": 9, "isInline": True}],
            [{"id": "L1", "name": "logo.png", "size": 9, "isInline": True}],
        ])
        graph = _Graph({
            ("GET", "/me/messages/D1"): (200, {**_D1, "isDraft": True}),
            ("GET", "/me/messages/D1/attachments"): lambda r: (200, {"value": next(listings)}),
            ("DELETE", "/me/messages/D1/attachments/A1"): (204, {}),
        })
        res = await _ms_tool(graph, monkeypatch).execute(
            "update_draft", draft_id="D1", remove_attachments=["all"], **_rt(tmp_path))
        assert res.success, res.error
        assert [r.url.path.rsplit("/", 1)[-1] for r in graph.requests if r.method == "DELETE"] == ["A1"]
        assert "Attachments: logo.png (inline image, 9 B)" in res.content

    async def test_nothing_is_removed_when_a_new_file_failed(self, monkeypatch, tmp_path, files):
        graph = _Graph({
            ("GET", "/me/messages/D1"): (200, {**_D1, "isDraft": True}),
            ("GET", "/me/messages/D1/attachments"): (200, {"value": [{"id": "A1", "name": "report.pdf"}]}),
            ("POST", "/me/messages/D1/attachments"): (500, {"error": {"code": "x", "message": "boom"}}),
        })
        res = await _ms_tool(graph, monkeypatch).execute(
            "update_draft", draft_id="D1", attachments=[str(files["csv"])],
            remove_attachments=["report.pdf"], **_rt(tmp_path))
        assert res.success and "NOT attached: data.csv" in res.content
        assert "NOT removed (still on the draft): report.pdf (kept because a new file failed" in res.content
        assert [r for r in graph.requests if r.method == "DELETE"] == []

    async def test_update_draft_unknown_removal_changes_nothing(self, monkeypatch, tmp_path):
        graph = _Graph({
            ("GET", "/me/messages/D1"): (200, {**_D1, "isDraft": True}),
            ("GET", "/me/messages/D1/attachments"): (200, {"value": [{"id": "A1", "name": "old.pdf"}]}),
        })
        res = await _ms_tool(graph, monkeypatch).execute(
            "update_draft", draft_id="D1", body="New", remove_attachments=["x.pdf"], **_rt(tmp_path))
        assert not res.success and "[1] old.pdf" in res.error and "Nothing was changed." in res.error
        assert all(r.method == "GET" for r in graph.requests)


async def test_outlook_session_refused_as_too_small_falls_back_to_one_post(monkeypatch, tmp_path, files):
    import captain_claw.tools.microsoft_mail as mod

    monkeypatch.setattr(mod, "_SMALL_ATTACHMENT_BYTES", 10)
    graph = _Graph({
        _NEW_DRAFT: (201, _D1),
        ("POST", "/me/messages/D1/attachments/createUploadSession"): (400, {"error": {
            "code": "ErrorAttachmentSizeShouldNotBeLessThanMinimumSize", "message": "too small"}}),
        ("POST", "/me/messages/D1/attachments"): (201, {"id": "AT1"}),
    })
    res = await _ms_tool(graph, monkeypatch).execute(
        "create_draft", to="ana@x.co", subject="Q3", body="x", allow_repeat=True,
        attachments=[str(files["csv"])], **_rt(tmp_path))
    assert res.success, res.error
    assert "Attachments: data.csv (22 B)" in res.content and "NOT attached" not in res.content
    (post,) = graph.of("POST", "/me/messages/D1/attachments")
    assert json.loads(post.content)["contentBytes"] == base64.b64encode(CSV).decode()
