"""gmail_compose's ``extra_headers`` — the RFC 3834 auto-reply markers.

``build_message_bytes`` / ``build_raw_message`` take ``extra_headers``: only
``Auto-Submitted`` and ``X-Auto-Response-Suppress``, added after the standard
headers. Anything else is a ValueError (so the parameter can never set a
recipient), and without it a message is byte for byte what it always was —
proved here against a copy of the builder as it was before the parameter, with
the MIME boundary's random source pinned so two builds can be compared.
"""

from __future__ import annotations

import base64
import email
import email.generator
import email.policy
import random
from email.message import EmailMessage
from types import MappingProxyType

import pytest

from captain_claw import gmail_compose
from captain_claw.mail_attachments import Attachment

AUTO = {"Auto-Submitted": "auto-replied", "X-Auto-Response-Suppress": "All"}
PDF = b"%PDF-1.4 quarterly report"


def _legacy_build_message_bytes(
    to="", cc="", bcc="", subject="", body="", html_body="", in_reply_to="",
    references="", attachments=None,
) -> bytes:
    """The builder before ``extra_headers`` existed — verbatim."""
    msg = EmailMessage(policy=email.policy.SMTP)
    if to:
        msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if subject:
        msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    plain = body or ""
    effective_html = html_body
    if not effective_html:
        effective_html = gmail_compose.text_to_html(plain)
    if not plain and html_body:
        plain = gmail_compose.html_to_text(html_body)

    msg.set_content(plain or " ")
    msg.add_alternative(effective_html, subtype="html")
    if attachments:
        from captain_claw import mail_attachments

        mail_attachments.add_to(msg, attachments)

    return msg.as_bytes()


def _pinned(monkeypatch, build, *args, seed=1234, **kwargs):
    """*build*(…) with the MIME boundaries drawn from a fixed seed."""
    monkeypatch.setattr(email.generator, "random", random.Random(seed))
    return build(*args, **kwargs)


def _decode_raw(raw: str):
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    return email.message_from_bytes(data, policy=email.policy.default)


CASES = {
    "plain": dict(to="bob@x.co", subject="Hello", body="Hi Bob"),
    "everything": dict(
        to="Bob <bob@x.co>", cc="carol@ok.com", bcc="dave@ok.com", subject="Re: Plans",
        body="Count me in\nsee you", html_body="<p>Count me <b>in</b></p>",
        in_reply_to="<orig@ok.com>", references="<a@ok.com> <orig@ok.com>",
    ),
    "unicode": dict(to="Željko Horvat <z@x.co>", subject="Poročilo — Q3", body="Pozdrav, Željko"),
    "html_only": dict(to="bob@x.co", subject="s", html_body="<div>Only <i>html</i></div>"),
    "files": dict(
        to="bob@x.co", subject="Report", body="See attached.",
        attachments=[Attachment("Poročilo Q3.pdf", "application/pdf", PDF),
                     Attachment("data.csv", "text/csv", b"a,b\n")],
    ),
    "empty": dict(),
}


# ── the default is byte-identical ────────────────────────────────────


class TestDefaultIsUnchanged:
    def test_the_seed_pins_the_boundary(self, monkeypatch):
        args = CASES["plain"]
        one = _pinned(monkeypatch, _legacy_build_message_bytes, **args)
        assert _pinned(monkeypatch, _legacy_build_message_bytes, **args) == one
        assert _pinned(monkeypatch, _legacy_build_message_bytes, seed=99, **args) != one

    @pytest.mark.parametrize("case", sorted(CASES))
    def test_message_bytes_match_the_old_builder(self, monkeypatch, case):
        args = CASES[case]
        legacy = _pinned(monkeypatch, _legacy_build_message_bytes, **args)
        assert _pinned(monkeypatch, gmail_compose.build_message_bytes, **args) == legacy
        assert _pinned(monkeypatch, gmail_compose.build_message_bytes,
                       extra_headers=None, **args) == legacy
        assert _pinned(monkeypatch, gmail_compose.build_message_bytes,
                       extra_headers={}, **args) == legacy

    @pytest.mark.parametrize("case", sorted(CASES))
    def test_raw_message_matches_the_old_builder(self, monkeypatch, case):
        args = CASES[case]
        legacy = _pinned(monkeypatch, _legacy_build_message_bytes, **args)
        want = base64.urlsafe_b64encode(legacy).decode("ascii").rstrip("=")
        assert _pinned(monkeypatch, gmail_compose.build_raw_message, **args) == want
        assert _pinned(monkeypatch, gmail_compose.build_raw_message,
                       extra_headers=None, **args) == want

    def test_no_auto_reply_header_unless_asked(self):
        msg = _decode_raw(gmail_compose.build_raw_message(**CASES["everything"]))
        assert msg["Auto-Submitted"] is None and msg["X-Auto-Response-Suppress"] is None


# ── the two allowed headers ──────────────────────────────────────────


class TestExtraHeaders:
    def test_the_allowed_names_are_exactly_the_auto_reply_markers(self):
        assert gmail_compose.EXTRA_HEADER_NAMES == frozenset(
            {"Auto-Submitted", "X-Auto-Response-Suppress"})

    def test_they_go_on_after_the_standard_headers(self):
        raw = gmail_compose.build_raw_message(
            to="Carol <carol@ok.com>", subject="Re: Plans", body="Count me in",
            in_reply_to="<orig@ok.com>", references="<orig@ok.com>", extra_headers=AUTO,
        )
        msg = _decode_raw(raw)
        assert msg["Auto-Submitted"] == "auto-replied"
        assert msg["X-Auto-Response-Suppress"] == "All"
        assert list(msg.keys())[:6] == ["To", "Subject", "In-Reply-To", "References",
                                        "Auto-Submitted", "X-Auto-Response-Suppress"]
        assert msg.get_content_type() == "multipart/alternative"
        assert msg.get_body(("plain",)).get_content().strip() == "Count me in"

    def test_nothing_else_changes(self, monkeypatch):
        """With the headers, the message is the old one plus exactly two lines."""
        args = CASES["everything"]
        legacy = _pinned(monkeypatch, _legacy_build_message_bytes, **args)
        built = _pinned(monkeypatch, gmail_compose.build_message_bytes,
                        extra_headers=AUTO, **args)
        added = b"Auto-Submitted: auto-replied\r\nX-Auto-Response-Suppress: All\r\n"
        assert added in built
        assert built.replace(added, b"", 1) == legacy

    def test_the_mapping_order_is_kept(self):
        raw = gmail_compose.build_raw_message(
            to="bob@x.co", subject="s", body="b",
            extra_headers={"X-Auto-Response-Suppress": "All", "Auto-Submitted": "auto-replied"},
        )
        keys = list(_decode_raw(raw).keys())
        assert keys.index("X-Auto-Response-Suppress") < keys.index("Auto-Submitted")

    def test_one_header_alone(self):
        msg = _decode_raw(gmail_compose.build_raw_message(
            to="bob@x.co", subject="s", body="b", extra_headers={"Auto-Submitted": "auto-replied"}))
        assert msg["Auto-Submitted"] == "auto-replied" and msg["X-Auto-Response-Suppress"] is None

    def test_a_read_only_mapping_is_fine(self):
        msg = _decode_raw(gmail_compose.build_raw_message(
            to="bob@x.co", subject="s", body="b", extra_headers=MappingProxyType(AUTO)))
        assert msg["Auto-Submitted"] == "auto-replied"

    def test_with_files_they_are_on_the_outer_message(self):
        raw = gmail_compose.build_message_bytes(**CASES["files"], extra_headers=AUTO)
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        assert msg.get_content_type() == "multipart/mixed"
        assert msg["Auto-Submitted"] == "auto-replied"
        assert msg["X-Auto-Response-Suppress"] == "All"
        assert {p.get_filename() for p in msg.iter_attachments()} == {"Poročilo Q3.pdf", "data.csv"}
        assert raw.count(b"Auto-Submitted:") == 1


# ── refusals ─────────────────────────────────────────────────────────


class TestRefusals:
    @pytest.mark.parametrize("name", [
        "To", "Cc", "Bcc", "From", "Reply-To", "Sender", "Subject", "Content-Type",
        "auto-submitted", "AUTO-SUBMITTED", "Auto-Submitted ", " Auto-Submitted",
        "x-auto-response-suppress", "X-Custom", "Precedence", "",
    ])
    @pytest.mark.parametrize("builder", ["bytes", "raw"])
    def test_any_other_name_is_refused(self, name, builder):
        build = (gmail_compose.build_message_bytes if builder == "bytes"
                 else gmail_compose.build_raw_message)
        with pytest.raises(ValueError, match="can't be added"):
            build(to="bob@x.co", subject="s", body="b", extra_headers={name: "evil@bad.com"})

    def test_one_bad_name_refuses_the_whole_set(self):
        with pytest.raises(ValueError, match="can't be added"):
            gmail_compose.build_message_bytes(
                to="bob@x.co", subject="s", body="b",
                extra_headers={**AUTO, "Bcc": "evil@bad.com"})

    def test_the_refusal_names_the_allowed_headers(self):
        with pytest.raises(ValueError) as exc:
            gmail_compose.build_message_bytes(to="bob@x.co", extra_headers={"Bcc": "x@y.co"})
        assert "Auto-Submitted" in str(exc.value) and "X-Auto-Response-Suppress" in str(exc.value)
        assert "x@y.co" not in str(exc.value)  # the value is never echoed

    @pytest.mark.parametrize("value", [
        "auto-replied\r\nBcc: evil@bad.com", "auto-replied\nBcc: evil@bad.com",
        "auto-replied\rBcc: evil@bad.com", "a\x0bb", "a b",
    ])
    def test_a_line_break_in_a_value_is_refused(self, value):
        for build in (gmail_compose.build_message_bytes, gmail_compose.build_raw_message):
            with pytest.raises(ValueError):
                build(to="bob@x.co", subject="s", body="b",
                      extra_headers={"Auto-Submitted": value})

    @pytest.mark.parametrize("value", ["", "   ", None, 1, b"auto-replied", ["All"]])
    def test_a_value_must_be_text(self, value):
        with pytest.raises(ValueError, match="needs a text value"):
            gmail_compose.build_message_bytes(
                to="bob@x.co", subject="s", body="b", extra_headers={"Auto-Submitted": value})

    @pytest.mark.parametrize("value", [
        [("Auto-Submitted", "auto-replied")], "Auto-Submitted: auto-replied",
        {("Auto-Submitted", "auto-replied")},
    ])
    def test_extra_headers_must_be_a_mapping(self, value):
        with pytest.raises(ValueError, match="mapping"):
            gmail_compose.build_message_bytes(to="bob@x.co", body="b", extra_headers=value)


# ── html_to_text: linear, and tags with "<" in quoted attributes ─────


@pytest.mark.parametrize("html,text", [
    ('<p>Hi <img alt="<3" src="https://cdn.example.com/heart.png"> Ana</p>', "Hi  Ana"),
    ('<a title="price < 10 EUR" href="x">Shop</a>', "Shop"),
    ('x <!-- <img src="old-banner.png"> --> y', "x  y"),
    ("a <!-- never closed", "a"),
])
def test_html_to_text_strips_whole_tags_and_comments(html, text):
    assert gmail_compose.html_to_text(html) == text


@pytest.mark.parametrize("junk", ["<" * 1_000_000, '<"' * 500_000, "<!--" * 250_000,
                                  ('<a "' + "x" * 100) * 10_000])
def test_html_to_text_is_linear(junk):
    import time

    started = time.monotonic()
    gmail_compose.html_to_text(junk)
    assert time.monotonic() - started < 2.0
