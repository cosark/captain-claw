"""Local files attached to outgoing email — google_mail (drafts and sends,
also through Flight Deck's send route) and microsoft_mail (drafts).

The tools take ``attachments``: a list of paths as the file tools take them —
a ``saved/`` path, a path a tool printed (``Saved to:``), an absolute path or
``vfs:<project>/<path>``. :func:`load` resolves each like the read tool does,
refuses credential files, keeps a shared-agent member inside their own files
(:func:`speaker.path_allowed`, on top of the registry's path rule) and reads
the bytes. Every check runs before anything is created or sent: one bad
attachment refuses the whole call.

Flight Deck gets the files from the agent as :func:`to_wire` dicts and
re-checks them with :func:`from_wire` before building the email itself.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import email
import email.policy
import hashlib
import mimetypes
import os
import re
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

# At most this many files per email.
MAX_ATTACHMENTS = 20
# Gmail refuses a message over 25 MB — counted on the encoded message, so the
# files themselves can be about 18 MB together.
GMAIL_MAX_MESSAGE_BYTES = 25 * 1024 * 1024
# Outlook: Graph takes up to 150 MB per file, but Exchange Online's default
# send limit is 35 MB encoded — a draft the user couldn't send is no help.
OUTLOOK_MAX_TOTAL_BYTES = 25 * 1024 * 1024

# A file name in a MIME header stays under this many UTF-8 bytes.
_MAX_NAME_BYTES = 180
# Control characters, Unicode line breaks and bidi overrides (they forge lines
# in a listing or disguise an extension).
_NAME_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x85  ‎‏‪-‮⁦-⁩]+")
_MIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$")
# Types the host's mimetypes table may not know.
_MIME_BY_SUFFIX = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".heic": "image/heic",
    ".webp": "image/webp", ".ics": "text/calendar", ".vcf": "text/vcard",
}

# Credentials never leave by email, whatever the request says: folders that
# hold keys, files that are keys or secrets by name, SQLite databases (Flight
# Deck's and the agent's hold OAuth tokens and keys), and anything in the
# agent's or Flight Deck's data folders outside the saved/ and VFS areas.
_SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"})
_SECRET_NAME_RE = re.compile(
    r"^(\.env(\..*)?|\.netrc|\.pgpass|\.git-credentials|\.npmrc|\.pypirc|agent_secret"
    r"|id_(rsa|dsa|ecdsa|ed25519)|credentials\.json|token\.json|client_secret.*\.json)$"
    r"|\.(pem|p12|pfx|jks|keystore)$",   # not .key: that is also a Keynote deck
    re.IGNORECASE,
)
_SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(frozen=True)
class Attachment:
    """One file to attach: the name the recipient sees, its type, its bytes."""

    filename: str
    mime_type: str
    data: bytes


class AttachmentError(ValueError):
    """An attachment that can't be used; the message is written for the model."""


# ── names, types, sizes ─────────────────────────────────────────────


def safe_filename(name: Any, fallback: str = "attachment") -> str:
    """*name* as an email attachment name: no folders, no control or bidi
    characters, at most :data:`_MAX_NAME_BYTES` UTF-8 bytes (the extension
    kept), *fallback* when nothing is left."""
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = " ".join(_NAME_CONTROL_RE.sub(" ", text).split()).strip(" .")
    if not text:
        return fallback
    if len(text.encode()) > _MAX_NAME_BYTES:
        suffix = Path(text).suffix if len(Path(text).suffix) <= 16 else ""
        budget = _MAX_NAME_BYTES - len(suffix.encode())
        stem = text[: len(text) - len(suffix)].encode()[:budget].decode("utf-8", "ignore")
        text = stem.rstrip() + suffix
    return text or fallback


def guess_mime(name: str) -> str:
    """The attachment's MIME type from its name; ``application/octet-stream``
    when unknown. ``message/*`` and ``multipart/*`` go as octet-stream: they
    can't be base64-encoded, and the extension still opens them."""
    suffix = Path(name).suffix.lower()
    mime = _MIME_BY_SUFFIX.get(suffix) or mimetypes.guess_type(name)[0] or ""
    if not _MIME_RE.match(mime) or mime.split("/", 1)[0].lower() in ("message", "multipart"):
        return "application/octet-stream"
    return mime.lower()


def clean_mime(mime: Any, name: str) -> str:
    """*mime* when it is a usable attachment type, else the one *name* implies."""
    if isinstance(mime, str) and _MIME_RE.match(mime.strip()):
        mime = mime.strip().lower()
        if mime.split("/", 1)[0] not in ("message", "multipart"):
            return mime
        return "application/octet-stream"
    return guess_mime(name)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def describe(atts: list[Attachment]) -> str:
    """``report.pdf (1.2 MB), data.csv (3 KB)`` — names only, never paths
    (a chat bridge sends files named on a ``Path:`` line)."""
    return ", ".join(f"{a.filename} ({human_size(len(a.data))})" for a in atts)


def unique_names(atts: list[Attachment]) -> list[Attachment]:
    """Two files with one name get `` (2)``, `` (3)``… so the recipient can
    tell them apart (and a later replace-by-name hits only one)."""
    taken: set[str] = set()
    out: list[Attachment] = []
    for a in atts:
        name, copy = a.filename, 1
        while name.casefold() in taken:
            copy += 1
            stem, dot, ext = a.filename.rpartition(".")
            name = f"{stem} ({copy}).{ext}" if dot and stem else f"{a.filename} ({copy})"
        taken.add(name.casefold())
        out.append(a if name == a.filename else Attachment(name, a.mime_type, a.data))
    return out


def encoded_size(atts: list[Attachment]) -> int:
    """Roughly what *atts* add to an email: base64 grows them by 4/3, plus
    line breaks and part headers."""
    return sum(len(a.data) * 4 // 3 + len(a.data) // 38 + 400 for a in atts)


def digest(atts: list[Attachment]) -> str:
    """sha256 over the names and bytes — part of a send's content hash, so a
    retried send with the same files is caught and different files aren't."""
    h = hashlib.sha256()
    for a in atts:
        h.update(a.filename.encode() + b"\0" + hashlib.sha256(a.data).digest())
    return h.hexdigest()


def add_to(msg: EmailMessage, atts: list[Attachment]) -> None:
    """Attach *atts* to an already-built message (it becomes multipart/mixed)."""
    for a in atts:
        maintype, _, subtype = a.mime_type.partition("/")
        msg.add_attachment(a.data, maintype=maintype, subtype=subtype, filename=a.filename)


# ── the tools' argument ─────────────────────────────────────────────


def parse_list(value: Any) -> list[str]:
    """The ``attachments`` argument as a list of non-empty strings. A single
    string is one path (a file name may contain commas)."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise AttachmentError("attachments must be a list of file paths.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AttachmentError("attachments must be a list of file paths (text).")
        if item.strip():
            out.append(item.strip())
    return out


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _data_homes() -> list[Path]:
    """The agent's and Flight Deck's own data folders (config, databases,
    tokens, other agents' workspaces, every user's VFS)."""
    homes = [Path.home() / ".captain-claw", Path.home() / ".captain-claw-fd"]
    for var in ("FD_DATA_DIR", "CAPTAIN_CLAW_FD_HOME"):
        value = os.environ.get(var, "").strip()
        if value:
            homes.append(Path(value).expanduser())
    try:
        from captain_claw import vfs

        base = vfs.vfs_base()
        if base.name == "vfs":
            homes.append(base.parent)   # the fd-data folder
    except Exception:
        pass
    out: list[Path] = []
    for h in homes:
        try:
            out.append(h.resolve())
        except (OSError, RuntimeError):
            continue
    return out


def _open_areas(runtime: dict[str, Any]) -> list[Path]:
    """Inside the data folders, where attachable files live: this agent's
    saved/ area, the caller's own VFS and the workflow run folder."""
    areas: list[Path] = []
    for key in ("_saved_base_path", "_workflow_run_dir"):
        if runtime.get(key):
            areas.append(Path(runtime[key]))
    try:
        from captain_claw import vfs

        areas.append(Path(vfs.user_root()))
    except Exception:
        pass
    out: list[Path] = []
    for a in areas:
        try:
            out.append(a.resolve())
        except (OSError, RuntimeError):
            continue
    return out


def credential_reason(path: Path, runtime: dict[str, Any] | None = None) -> str:
    """Why *path* (resolved) may not be emailed, or ""."""
    parts = [p.lower() for p in path.parts[:-1]]
    if any(p in _SECRET_DIRS for p in parts):
        return "it is in a folder that holds keys or credentials"
    if _SECRET_NAME_RE.search(path.name):
        return "it looks like a key or a secrets file"
    try:
        with path.open("rb") as fh:
            if fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC:
                return ("it is a database (databases can hold tokens and keys) — export the "
                        "data you need to a CSV or a spreadsheet and attach that")
    except OSError:
        pass
    if any(_within(path, home) for home in _data_homes()) and not any(
        _within(path, area) for area in _open_areas(runtime or {})
    ):
        return ("it is in the agent's or Flight Deck's own data folder — only files under "
                "saved/ or the shared VFS can be attached")
    return ""


def resolve_path(value: str, runtime: dict[str, Any]) -> Path | None:
    """The existing file *value* names, found the way the read tool finds it:
    ``vfs:`` paths in the shared tree; else absolute, under the workspace,
    under ``saved/``, under the process directory, in the workflow run folder,
    then through the file registry. None when there is no such file."""
    from captain_claw.vfs import is_vfs_path, resolve_vfs_path

    if is_vfs_path(value):
        target = resolve_vfs_path(value)
        return Path(target).resolve() if target is not None and Path(target).is_file() else None

    raw = Path(value).expanduser()
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        runtime_base = runtime.get("_runtime_base_path")
        saved_base = runtime.get("_saved_base_path")
        if runtime_base:
            candidates.append(Path(runtime_base) / raw)
        if saved_base:
            parts = raw.parts
            under_saved = Path(*parts[1:]) if parts and parts[0] == "saved" and len(parts) > 1 else raw
            candidates.append(Path(saved_base) / under_saved)
        candidates.append(Path.cwd() / raw)
    run_dir = runtime.get("_workflow_run_dir")
    if run_dir:
        rel = Path(*raw.parts[1:]) if raw.is_absolute() and len(raw.parts) > 1 else raw
        candidates += [Path(run_dir) / rel, Path(run_dir) / raw.name]
    for cand in candidates:
        try:
            if cand.is_file():
                return cand.resolve()
        except OSError:
            continue
    registry = runtime.get("_file_registry")
    if registry is not None:
        try:
            physical = registry.resolve(value)
        except Exception:
            physical = None
        if physical:
            cand = Path(physical).expanduser()
            if cand.is_file():
                return cand.resolve()
    return None


async def load(
    values: list[str], runtime: dict[str, Any], *, max_total_bytes: int,
    limit_label: str,
) -> list[Attachment]:
    """Resolve and read every file of *values*; :class:`AttachmentError` on
    the first one that can't be attached. *max_total_bytes* bounds the files
    together (*limit_label* names the limit in the message)."""
    from captain_claw import speaker as _speaker

    if len(values) > MAX_ATTACHMENTS:
        raise AttachmentError(f"Too many attachments ({len(values)}) — at most {MAX_ATTACHMENTS} per email.")
    found: list[tuple[str, Path]] = []
    total = 0
    for value in values:
        if "\x00" in value:
            raise AttachmentError("An attachment path contains a NUL character.")
        path = resolve_path(value, runtime)
        if path is None:
            # A member's value is already a host path (the registry resolved it):
            # name only the file, never the folders.
            shown = Path(value).name if _speaker.current() is not None else value
            raise AttachmentError(
                f"Attachment not found: {shown}. Name a file (not a folder): the path a tool "
                "printed (e.g. its 'Saved to:' line), a saved/ path, or vfs:<project>/<path>."
            )
        why = credential_reason(path, runtime)
        if why:
            raise AttachmentError(f"'{path.name}' can't be attached: {why}.")
        if _speaker.current() is not None and not _speaker.path_allowed(path):
            raise AttachmentError(_speaker.PATH_REFUSED_PREFIX + "that file is outside your folders")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AttachmentError(f"'{path.name}' can't be read ({exc.strerror or type(exc).__name__}).") from exc
        if size == 0:
            raise AttachmentError(f"'{path.name}' is empty — nothing to attach.")
        total += size
        if total > max_total_bytes:
            raise AttachmentError(
                f"The attachments are too large together ({human_size(total)} and counting) — "
                f"{limit_label}. Upload big files to Drive / OneDrive and put the share link in "
                "the body instead."
            )
        found.append((value, path))
    out: list[Attachment] = []
    for _value, path in found:
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise AttachmentError(f"'{path.name}' can't be read ({exc.strerror or type(exc).__name__}).") from exc
        name = safe_filename(path.name)
        out.append(Attachment(name, guess_mime(name), data))
    return unique_names(out)


# ── the files already in a message (a draft being revised) ──────────


@dataclass(eq=False)   # parts compare by identity
class FilePart:
    """One file part of a parsed message: the part itself (kept byte-for-byte
    when the message is rebuilt), the multipart it sits in, its name, whether
    it is an inline image of the text (multipart/related, or inline with a
    Content-ID) and its decoded size."""

    part: EmailMessage
    parent: EmailMessage | None
    name: str
    inline: bool
    size: int


def parse_message(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.SMTP)


def message_files(msg: EmailMessage) -> list[FilePart]:
    """The message's file parts in MIME order — the order Gmail's
    read_message numbers them in. A forwarded email (message/*) is one file."""
    out: list[FilePart] = []

    def walk(part: EmailMessage, parent: EmailMessage | None, in_related: bool) -> None:
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            related = ctype == "multipart/related"
            for i, child in enumerate(part.get_payload() or []):
                walk(child, part, in_related or (related and i > 0))
            return
        try:
            name = part.get_filename() or ""
        except Exception:
            name = ""
        disposition = (part.get_content_disposition() or "").lower()
        is_text = ctype in ("text/plain", "text/html")
        if name:
            is_file = not is_text or disposition == "attachment"
        else:
            is_file = not ctype.startswith("text/") and parent is not None
        if not is_file:
            return
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            data = b""
        if part.get_content_maintype() == "message":
            data = part.as_bytes()
        if not name:
            if not data:
                return
            ext = mimetypes.guess_extension(ctype) or ""
            name = f"{'inline' if ctype.startswith('image/') else 'attachment'}-{len(out) + 1}{ext}"
        inline = in_related or (disposition == "inline" and bool(part.get("Content-ID")))
        out.append(FilePart(part, parent, safe_filename(name, f"attachment-{len(out) + 1}"),
                            inline, len(data)))

    walk(msg, None, False)
    return out


def detach(fp: FilePart) -> None:
    """Take *fp* out of its message."""
    if fp.parent is not None:
        fp.parent.set_payload([p for p in fp.parent.get_payload() if p is not fp.part])


def describe_parts(files: list[FilePart]) -> str:
    return ", ".join(
        f"{f.name} ({'inline image, ' if f.inline else ''}{human_size(f.size)})" for f in files
    )


# ── agent → Flight Deck ─────────────────────────────────────────────


def to_wire(atts: list[Attachment]) -> list[dict[str, str]]:
    return [
        {"filename": a.filename, "mime_type": a.mime_type,
         "data": base64.b64encode(a.data).decode("ascii")}
        for a in atts
    ]


def from_wire(raw: Any, *, max_total_bytes: int) -> list[Attachment]:
    """Flight Deck's check of the agent's attachments (any shape): names
    cleaned, types checked, base64 decoded strictly, count and total bounded.
    :class:`AttachmentError` with a message for the agent."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AttachmentError("attachments must be a list")
    if len(raw) > MAX_ATTACHMENTS:
        raise AttachmentError(f"Too many attachments ({len(raw)}) — at most {MAX_ATTACHMENTS} per email.")
    out: list[Attachment] = []
    total = 0
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise AttachmentError(f"attachment {i} must be an object")
        data_b64 = item.get("data")
        if not isinstance(data_b64, str) or not data_b64:
            raise AttachmentError(f"attachment {i} has no data")
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AttachmentError(f"attachment {i}: data is not valid base64") from exc
        if not data:
            raise AttachmentError(f"attachment {i} is empty")
        total += len(data)
        if total > max_total_bytes:
            raise AttachmentError(
                f"The attachments are too large together ({human_size(total)} and counting)."
            )
        name = safe_filename(item.get("filename"), f"attachment-{i}")
        out.append(Attachment(name, clean_mime(item.get("mime_type"), name), data))
    return unique_names(out)
