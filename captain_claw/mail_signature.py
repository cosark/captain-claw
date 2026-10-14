"""The agentic signature — text a Flight Deck user sets in Connections → Google
and Connections → Microsoft that goes at the end of every email and draft
their agents write from that account.

Flight Deck keeps it per user (:data:`GOOGLE_KEY` / :data:`MICROSOFT_KEY` —
server-owned ``user_settings``, so ``/fd/settings`` can't write them, and a
disconnect leaves them in place) and hands it to the agent in the provider's
``agent_status`` answer as ``mail_signature`` (absent = none, which is also
what an older deck says). The agents' mail tools add it to every draft they
create or rewrite; Flight Deck adds it to every Gmail send it composes. A
draft sent as it is already carries it.

Adding it twice is a no-op: :func:`apply` leaves a body that already ends
with it. The signature is plain text; the HTML part gets an escaped copy.
"""

from __future__ import annotations

import html as html_module
import re
import unicodedata
from typing import Any

from captain_claw import gmail_compose

GOOGLE_KEY = "google_oauth:mail_signature"
MICROSOFT_KEY = "microsoft_oauth:mail_signature"

MAX_CHARS = 1000

_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def normalize(value: Any) -> str:
    """A signature as stored: line breaks as ``\\n``, control characters
    dropped, trailing spaces and surrounding blank lines trimmed. None is
    "". Raises ValueError for anything but text, or text over
    :data:`MAX_CHARS`."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("signature must be text")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc"
    )
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    if len(text) > MAX_CHARS:
        raise ValueError(
            f"signature can be at most {MAX_CHARS} characters (got {len(text)})"
        )
    return text


async def load(db: Any, user_id: str, key: str) -> str:
    """*user_id*'s stored signature under *key* — "" when none is stored or
    the stored value is unusable (never raises)."""
    try:
        return normalize(await db.get_setting(user_id, key))
    except Exception:
        return ""


def _squash(text: str) -> str:
    return " ".join((text or "").split())


def _ends_with(text: str, signature: str) -> bool:
    return _squash(text).endswith(_squash(signature))


def append_text(body: str, signature: str) -> str:
    """*body* with *signature* after a blank line (just the signature when
    the body is empty) — unchanged when it already ends with it."""
    if not signature or _ends_with(body, signature):
        return body
    base = (body or "").rstrip()
    return f"{base}\n\n{signature}" if base else signature


def signature_html(signature: str) -> str:
    """The signature as an HTML block, after an empty line."""
    escaped = html_module.escape(signature).replace("\n", "<br>")
    return f"<div><br></div><div>{escaped}</div>"


def append_html(html_body: str, signature: str) -> str:
    """*html_body* with the signature block at its end (inside ``<body>``
    when it has one) — unchanged when its text already ends with it."""
    if not signature or _ends_with(gmail_compose.html_to_text(html_body or ""), signature):
        return html_body
    block = signature_html(signature)
    closes = list(_BODY_CLOSE_RE.finditer(html_body or ""))
    if closes:
        at = closes[-1].start()
        return html_body[:at] + block + html_body[at:]
    return (html_body or "") + block


def apply(body: str, html_body: str, signature: str) -> tuple[str, str]:
    """``(body, html_body)`` with *signature* at the end of each part that
    has content; with neither, the signature becomes the plain body."""
    if not signature:
        return body, html_body
    has_text = bool((body or "").strip())
    has_html = bool((html_body or "").strip())
    if not has_text and not has_html:
        return signature, html_body
    if has_text:
        body = append_text(body, signature)
    if has_html:
        html_body = append_html(html_body, signature)
    return body, html_body
