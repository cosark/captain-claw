"""Topic auto-replies for Gmail — the per-user config.

One server-owned user setting, :data:`AUTOREPLY_KEY` (``google_oauth:`` — so
``/fd/settings`` can't read or write it, and no script or agent can switch it
on)::

    {"enabled": false, "agent_ref": "", "daily_limit": 10, "never_disclose": [],
     "topics": [],
     "google_sub": "", "account_email": "", "enabled_at": "",
     "disabled_reason": "", "updated_at": ""}

* ``enabled`` — OFF by default. :func:`apply_patch` never turns it on: it only
  reports that the user asked (``wants_enable``), and the route turns it on with
  :func:`enable` once every precondition and the Gmail baseline passed;
* ``agent_ref`` — the one agent that writes the replies (its ownership and
  eligibility are the route's to check);
* ``daily_limit`` — auto-replies per rolling 24 hours (default 10, 1..50);
* ``never_disclose`` — at most 50 owner terms, 2..80 characters each, that no
  reply may contain (they go to the guard only, never to a model);
* ``topics`` — at most 10, at most 5 of them on (see :class:`Topic`);
* the rest is server-managed — the Google account bound at enable time, when,
  why it was last turned off, when it was saved — and ignored on a PUT, as is
  every topic's ``dry_run``.

:func:`load_autoreply_config` mirrors ``load_gmail_send_policy``: nothing
stored, or a record that is unreadable or out of range anywhere, reads as the
defaults — auto-replies OFF.

Narrowness, the part enforced here:

* **lint** (:func:`lint_topic`, 400 "Too broad: …") — a topic's name and
  "Emails that…" text, case- and diacritic-folded, may not start with a
  catch-all word (any, all, every, general, other, sve, svi, bilo, općenito …)
  nor say "all emails", "any question", "svi mailovi", "bilo što" …; the text
  needs at least 6 words and the topic at least one example;
* **match hash** (:func:`match_hash`) — sha256 over what the classifier sees
  (name, about, not_this, examples, counter_examples, senders); the brief and
  the knowledge sources don't change what matches, so they are left out;
* **breadth gate** (:func:`dry_run_gate`, 409) — a topic is switched on only
  with a breadth test (dry run on the owner's recent mail) for its current
  match hash, at most 30 days old, that matched at most 25 % of the eligible
  mail (10–25 % passes with a warning; under 5 eligible messages passes with a
  note). Editing a matching field of a topic that is on turns it off, and the
  PUT answers with :data:`RERUN_NOTICE`.

So a topic that is on always has a breadth test of its current wording that
did not match too much (only its age is let go: a topic that is on keeps
running past its test's 30 days). Every way that stops being true switches the
topic off — an edit of what it matches (:func:`apply_patch`), a re-test that
matched too much (:func:`record_dry_run`), the drift breaker
(:func:`disable_topic`, which also drops the test, so switching it on again
needs a new one) — and the loader reads a stored record that breaks it as off.
A topic's breadth test is only ever written by :func:`record_dry_run`: a PUT
can't set, move or bring one back.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from captain_claw import gmail_compose
from captain_claw.flight_deck import agent_sharing
from captain_claw.logging import get_logger

log = get_logger(__name__)


AUTOREPLY_KEY = "google_oauth:gmail_autoreply"  # per-user, server-owned

DEFAULT_DAILY_LIMIT = 10
MAX_DAILY_LIMIT = 50
MAX_TOPICS = 10
MAX_ENABLED_TOPICS = 5
MAX_NEVER_DISCLOSE = 50
NEVER_DISCLOSE_CHARS = (2, 80)

# Topic fields, in characters (examples: per example) and counts.
NAME_CHARS = (3, 60)
ABOUT_CHARS = (20, 400)
ABOUT_MIN_WORDS = 6
NOT_THIS_MAX_CHARS = 400
EXAMPLE_CHARS = (10, 300)
MAX_EXAMPLES = 5
MAX_COUNTER_EXAMPLES = 5
MAX_TOPIC_SENDERS = 50
BRIEF_CHARS = (50, 4000)
FOLDER_MAX_CHARS = 512

# The breadth gate.
DRY_RUN_MAX_AGE = timedelta(days=30)
MAX_MATCH_RATE = 0.25
WARN_MATCH_RATE = 0.10
MIN_ELIGIBLE = 5
TOO_LITTLE_MAIL = "too little mail to judge breadth"
RERUN_NOTICE = "re-run the breadth test"
# A re-test of a topic that is on matched too much: it was switched off.
TOO_BROAD_NOTICE = ("switched off: it matched too much of your recent mail — make it "
                    "narrower, then re-run the breadth test")

_CLOCK_SKEW = timedelta(minutes=5)
_MAX_DRY_RUN_MESSAGES = 10_000
_MAX_MANAGED_CHARS = 320

_TOPIC_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Bumped only if what goes into the hash changes — every topic then needs a
# fresh breadth test, which is the safe direction.
_HASH_VERSION = "gmail_autoreply.match.v1"


class ConfigError(ValueError):
    """An update the config can't take. ``status`` is the HTTP answer — 400
    (invalid, too broad), 409 (a topic switched on without a passing breadth
    test) or 404 (no such topic) — and the message is the readable detail."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ── the typed config ────────────────────────────────────────────────


@dataclass(frozen=True)
class DryRun:
    """A topic's last breadth test (server-managed)."""

    match_hash: str = ""   # the topic's match hash the test ran against
    at: str = ""           # ISO time it ran
    eligible: int = 0      # recent inbox messages that passed the prefilters
    matched: int = 0       # of those, classified as this topic
    rate: float = 0.0      # matched / eligible
    since: str = ""        # when THIS wording was first tested (a re-test keeps it)

    def to_json(self) -> dict[str, Any]:
        return {"match_hash": self.match_hash, "at": self.at, "eligible": self.eligible,
                "matched": self.matched, "rate": self.rate, "since": self.since}


@dataclass(frozen=True)
class TopicSources:
    """What the writer may look things up in. ``folder`` is a VFS folder,
    canonically ``vfs:<project>/<path>`` (a project's root: ``vfs:<project>/``)
    — resolve it with ``vfs.resolve_under(owner, "", folder)``; "" = none."""

    folder: str = ""
    insights: bool = False
    playbooks: bool = True
    web_search: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"folder": self.folder, "insights": self.insights,
                "playbooks": self.playbooks, "web_search": self.web_search}


@dataclass(frozen=True)
class Topic:
    """One narrow kind of email the agent may answer on its own."""

    id: str                                 # t_<hex8>, server-assigned, stable
    name: str
    about: str                              # "Emails that…"
    examples: tuple[str, ...]               # should match (1..5)
    brief: str                              # what the agent may say — PUBLIC
    enabled: bool = False
    not_this: str = ""
    counter_examples: tuple[str, ...] = ()  # near-misses that should not match
    senders: tuple[str, ...] = ()           # addresses / @domains; () = any verified sender
    sources: TopicSources = field(default_factory=TopicSources)
    dry_run: DryRun = field(default_factory=DryRun)

    @property
    def match_hash(self) -> str:
        return match_hash(self)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "about": self.about,
            "not_this": self.not_this,
            "examples": list(self.examples),
            "counter_examples": list(self.counter_examples),
            "senders": list(self.senders),
            "brief": self.brief,
            "sources": self.sources.to_json(),
            "dry_run": self.dry_run.to_json(),
        }


@dataclass(frozen=True)
class AutoReplyConfig:
    """A user's topic auto-reply config (the defaults: off, no topics)."""

    enabled: bool = False
    agent_ref: str = ""
    daily_limit: int = DEFAULT_DAILY_LIMIT
    never_disclose: tuple[str, ...] = ()
    topics: tuple[Topic, ...] = ()
    # Server-managed — never taken from a PUT.
    google_sub: str = ""
    account_email: str = ""
    enabled_at: str = ""
    disabled_reason: str = ""
    updated_at: str = ""

    def topic(self, topic_id: str) -> Topic | None:
        for t in self.topics:
            if t.id == topic_id:
                return t
        return None

    @property
    def enabled_topics(self) -> tuple[Topic, ...]:
        return tuple(t for t in self.topics if t.enabled)

    def to_json(self) -> dict[str, Any]:
        """The stored record."""
        return {
            "enabled": self.enabled,
            "agent_ref": self.agent_ref,
            "daily_limit": self.daily_limit,
            "never_disclose": list(self.never_disclose),
            "topics": [t.to_json() for t in self.topics],
            "google_sub": self.google_sub,
            "account_email": self.account_email,
            "enabled_at": self.enabled_at,
            "disabled_reason": self.disabled_reason,
            "updated_at": self.updated_at,
        }

    def to_view(self, *, now: datetime | None = None) -> dict[str, Any]:
        """The owner's view: the record without the bound Google ``sub``, and
        each topic with its breadth ``gate`` (whether it may be switched on)."""
        view = self.to_json()
        view.pop("google_sub")
        view["topics"] = [{**t.to_json(), "gate": dry_run_gate(t, now=now).to_json()}
                          for t in self.topics]
        return view


def default_config() -> AutoReplyConfig:
    return AutoReplyConfig()


# ── folding, lint, match hash ───────────────────────────────────────


# Letters NFKD leaves whole (Croatian đ above all).
_FOLD_EXTRA = str.maketrans({"đ": "d", "ł": "l", "ø": "o", "ı": "i", "æ": "ae", "œ": "oe"})


def fold(text: str) -> str:
    """*text* case- and diacritic-folded: ``"Općenito Đak"`` → ``"opcenito dak"``."""
    t = unicodedata.normalize("NFKD", str(text or "").casefold().translate(_FOLD_EXTRA))
    return "".join(ch for ch in t if not unicodedata.combining(ch))


_BROAD_FIRST_WORDS = frozenset({
    "any", "all", "every", "anything", "everything", "general", "misc", "other",
    "whatever", "sve", "svi", "bilo", "opcenito",
})
# Matched on folded text with "e-mail" written "email"; a phrase starts at a
# word. "svi mailovi" in any case ("odgovori na sve mailove", "svim mailovima").
_BROAD_PHRASE_RE = re.compile(
    r"(?<![\w-])((?:all e?mails|any e?mail|any question|anything else|everything"
    r"|general inquir|sv(?:i|e|ih|im|ima) e?mailov|sva pitanja|bilo sto)\w*)"
)
_EMAIL_WORD_RE = re.compile(r"(?<![\w-])e[\s-]?mail")
# A word; a hyphenated compound ("all-hands") is one.
_FIRST_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
_WORD_RE = re.compile(r"[^\W_]+")


def _lint_text(label: str, text: str) -> str:
    folded = " ".join(_EMAIL_WORD_RE.sub("email", fold(text)).split())
    first = _FIRST_WORD_RE.search(folded)
    if first and first.group(0) in _BROAD_FIRST_WORDS:
        return (f'Too broad: the {label} starts with "{first.group(0)}". Name one concrete '
                "subject and the kind of request.")
    phrase = _BROAD_PHRASE_RE.search(folded)
    if phrase:
        return (f'Too broad: the {label} says "{phrase.group(1)}". Name one concrete '
                "subject and the kind of request.")
    return ""


def lint_topic(name: str, about: str, examples: Sequence[str]) -> str:
    """The "Too broad: …" reason the topic is refused with; "" when it reads
    narrow enough. Mirrored by the Connections UI's ``validateTopic``."""
    for label, text in (("name", name), ("description", about)):
        reason = _lint_text(label, text)
        if reason:
            return reason
    if len(_WORD_RE.findall(about or "")) < ABOUT_MIN_WORDS:
        return (f"Too broad: describe the emails in at least {ABOUT_MIN_WORDS} words — "
                "the subject and the kind of request.")
    if not [e for e in examples or () if str(e).strip()]:
        return "Too broad: add at least one example of an email that should match."
    return ""


def match_hash(topic: Topic) -> str:
    """sha256 (hex) over everything that decides what the topic matches: name,
    about, not_this, examples, counter_examples, senders. Not the brief, the
    sources, the switch or the id — editing those keeps a breadth test valid."""
    material = [_HASH_VERSION, topic.name, topic.about, topic.not_this,
                list(topic.examples), list(topic.counter_examples), list(topic.senders)]
    blob = json.dumps(material, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── the breadth gate ────────────────────────────────────────────────


@dataclass(frozen=True)
class Gate:
    """Whether a topic may be switched on: ``reason`` when not; a ``note`` or a
    ``warning`` to show when it may."""

    ok: bool
    reason: str = ""
    note: str = ""
    warning: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "note": self.note,
                "warning": self.warning}


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _iso(now: datetime | None) -> str:
    return _now(now).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _too_broad(dry: DryRun) -> bool:
    """Did the breadth test *dry* match too much of the eligible mail? Under
    :data:`MIN_ELIGIBLE` eligible messages there is too little to tell."""
    return dry.eligible >= MIN_ELIGIBLE and dry.matched / dry.eligible > MAX_MATCH_RATE


def _tested_now(topic: Topic) -> bool:
    """Is *topic*'s breadth test one of its current wording?"""
    return bool(topic.dry_run.match_hash) and topic.dry_run.match_hash == topic.match_hash


def dry_run_gate(topic: Topic, *, now: datetime | None = None) -> Gate:
    """The breadth gate for switching *topic* on: a breadth test of its current
    wording, at most 30 days old, that matched at most 25 % of the eligible
    mail. Under :data:`MIN_ELIGIBLE` eligible messages it passes with a note;
    10–25 % passes with a warning."""
    dry = topic.dry_run
    if not dry.match_hash or not dry.at:
        return Gate(False, reason="run the breadth test on your recent mail first")
    if dry.match_hash != topic.match_hash:
        return Gate(False, reason="the topic changed since its breadth test — re-run it")
    at = _parse_iso(dry.at)
    age = _now(now) - at if at is not None else None
    if age is None or age < -_CLOCK_SKEW or age > DRY_RUN_MAX_AGE:
        return Gate(False, reason="the breadth test is more than 30 days old — re-run it")
    if dry.eligible < MIN_ELIGIBLE:
        return Gate(True, note=TOO_LITTLE_MAIL)
    rate = dry.matched / dry.eligible
    pct = round(rate * 100)
    if _too_broad(dry):
        return Gate(False, reason=(
            f"it matched {pct}% of your recent mail (at most {round(MAX_MATCH_RATE * 100)}%"
            " may match) — make it narrower"))
    if rate >= WARN_MATCH_RATE:
        return Gate(True, warning=(
            f"it matched {pct}% of your recent mail — consider making it narrower"))
    return Gate(True)


# ── field validation (shared by the PUT parser and the loader) ──────


def _strip_controls(text: str) -> str:
    return "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))


def _one_line(text: str) -> str:
    return " ".join(_strip_controls(text).split())


def _multi_line(text: str) -> str:
    t = _strip_controls(text.replace("\r\n", "\n").replace("\r", "\n"))
    t = "\n".join(line.rstrip() for line in t.split("\n")).strip()
    return re.sub(r"\n{3,}", "\n\n", t)


def _length_detail(what: str, lo: int, hi: int, got: int) -> str:
    if lo <= 0:
        return f"{what} can be at most {hi} characters (got {got})"
    return f"{what} must be {lo} to {hi} characters (got {got})"


def _text(value: Any, what: str, lo: int, hi: int, *, multiline: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ConfigError(f"{what} must be text")
    text = _multi_line(value) if multiline else _one_line(value)
    if not lo <= len(text) <= hi:
        raise ConfigError(_length_detail(what, lo, hi, len(text)))
    return text


def _texts(value: Any, label: str, noun: str, max_count: int) -> tuple[str, ...]:
    """A list of example emails: one line each, blank ones dropped."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{label}: {noun}s must be a list of texts")
    lo, hi = EXAMPLE_CHARS
    out: list[str] = []
    for item in value:
        text = _one_line(item)
        if not text:
            continue
        if not lo <= len(text) <= hi:
            raise ConfigError(_length_detail(f"{label}: each {noun}", lo, hi, len(text)))
        out.append(text)
    if len(out) > max_count:
        raise ConfigError(f"{label}: at most {max_count} {noun}s (got {len(out)})")
    return tuple(out)


def _bool(value: Any, what: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{what} must be true or false")
    return value


def _daily_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_DAILY_LIMIT
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 1 <= value <= MAX_DAILY_LIMIT):
        raise ConfigError(f"daily_limit must be a whole number from 1 to {MAX_DAILY_LIMIT}")
    return value


def _agent_ref(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError("agent_ref must be text")
    ref = value.strip()
    if not ref:
        return ""
    try:
        agent_sharing.parse_ref(ref)
    except ValueError:
        raise ConfigError("agent_ref isn't an agent of this Flight Deck") from None
    return ref


def _never_disclose(value: Any) -> tuple[str, ...]:
    """Trimmed terms, blanks dropped, repeats (case and diacritics folded)
    dropped. Error details never echo a term."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError("never_disclose must be a list of terms")
    lo, hi = NEVER_DISCLOSE_CHARS
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ConfigError("never_disclose must be a list of terms")
        term = _one_line(item)
        if not term:
            continue
        if not lo <= len(term) <= hi:
            raise ConfigError(_length_detail("each never_disclose term", lo, hi, len(term)))
        key = fold(term)
        if key not in seen:
            seen.add(key)
            out.append(term)
    if len(out) > MAX_NEVER_DISCLOSE:
        raise ConfigError(
            f"never_disclose can hold at most {MAX_NEVER_DISCLOSE} terms (got {len(out)})")
    return tuple(out)


def _senders(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{label}: senders must be a list of addresses or @domains")
    try:
        out = gmail_compose.normalize_allowlist(list(value))
    except ValueError:
        bad: list[str] = []
        for entry in value:
            try:
                gmail_compose.normalize_allowlist([entry])
            except ValueError:
                bad.append(entry.strip() if isinstance(entry, str) else repr(entry)[:40])
        raise ConfigError(
            f"{label}: invalid sender(s): " + ", ".join(bad[:10])
            + (" …" if len(bad) > 10 else "")
            + ". Use an address (alice@example.com) or a domain (@example.com)."
        ) from None
    if len(out) > MAX_TOPIC_SENDERS:
        raise ConfigError(
            f"{label}: senders can hold at most {MAX_TOPIC_SENDERS} entries (got {len(out)})")
    return tuple(out)


def _folder(value: Any, label: str) -> str:
    """A VFS folder as ``vfs:<project>/<path>`` ("" = none). Only its shape is
    checked here — the route resolves it inside the user's files."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{label}: the folder must be text")
    raw = value.strip().replace("\\", "/")
    if not raw:
        return ""
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in raw):
        raise ConfigError(f"{label}: the folder has control characters")
    if raw[:4].lower() == "vfs:":
        raw = raw[4:]
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ConfigError(
            f"{label}: the folder must be a folder in your files (vfs:<project>/<folder>)")
    folder = f"vfs:{parts[0]}/" + "/".join(parts[1:])
    if len(folder) > FOLDER_MAX_CHARS:
        raise ConfigError(_length_detail(f"{label}: the folder", 0, FOLDER_MAX_CHARS,
                                         len(folder)))
    return folder


def _sources(value: Any, label: str) -> TopicSources:
    default = TopicSources()
    if value is None:
        return default
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label}: sources must be an object")
    return TopicSources(
        folder=_folder(value.get("folder"), label),
        insights=_bool(value.get("insights"), f"{label}: sources.insights", default.insights),
        playbooks=_bool(value.get("playbooks"), f"{label}: sources.playbooks",
                        default.playbooks),
        web_search=_bool(value.get("web_search"), f"{label}: sources.web_search",
                         default.web_search),
    )


def _label(raw: Mapping[str, Any], index: int) -> str:
    name = raw.get("name")
    if isinstance(name, str) and _one_line(name):
        return f'Topic "{_one_line(name)[:60]}"'
    return f"Topic {index + 1}"


def _topic_fields(raw: Any, index: int) -> dict[str, Any]:
    """The user-written fields of one topic, normalized and range-checked
    (no lint, no id, no dry run)."""
    if not isinstance(raw, Mapping):
        raise ConfigError(f"Topic {index + 1} must be an object")
    label = _label(raw, index)
    return {
        "name": _text(raw.get("name"), f"{label}: the name", *NAME_CHARS),
        "enabled": _bool(raw.get("enabled"), f"{label}: enabled", False),
        "about": _text(raw.get("about"), f"{label}: the description", *ABOUT_CHARS),
        "not_this": _text(raw.get("not_this"), f"{label}: not this", 0, NOT_THIS_MAX_CHARS),
        "examples": _texts(raw.get("examples"), label, "example", MAX_EXAMPLES),
        "counter_examples": _texts(raw.get("counter_examples"), label, "near-miss example",
                                   MAX_COUNTER_EXAMPLES),
        "senders": _senders(raw.get("senders"), label),
        "brief": _text(raw.get("brief"), f"{label}: what the agent may say", *BRIEF_CHARS,
                       multiline=True),
        "sources": _sources(raw.get("sources"), label),
    }


# ── the stored record → config ──────────────────────────────────────


def _managed_str(value: Any, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > _MAX_MANAGED_CHARS:
        raise ValueError(f"{what} out of range")
    return value


def _count(value: Any, what: str) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= _MAX_DRY_RUN_MESSAGES):
        raise ValueError(f"{what} out of range")
    return value


def _stored_dry_run(value: Any) -> DryRun:
    if value is None:
        return DryRun()
    if not isinstance(value, Mapping):
        raise ValueError("dry_run is not an object")
    digest = _managed_str(value.get("match_hash"), "dry_run.match_hash")
    at = _managed_str(value.get("at"), "dry_run.at")
    eligible = _count(value.get("eligible", 0), "dry_run.eligible")
    matched = _count(value.get("matched", 0), "dry_run.matched")
    rate = value.get("rate", 0.0)
    since = _managed_str(value.get("since", ""), "dry_run.since")
    if since and _parse_iso(since) is None:
        raise ValueError("dry_run out of range")
    if ((digest and not _HASH_RE.match(digest)) or (at and _parse_iso(at) is None)
            or matched > eligible or not isinstance(rate, (int, float))
            or isinstance(rate, bool) or not 0.0 <= rate <= 1.0):
        raise ValueError("dry_run out of range")
    return DryRun(match_hash=digest, at=at, eligible=eligible, matched=matched,
                  rate=float(rate), since=since)


def _stored_topic(raw: Any, index: int) -> Topic:
    fields = _topic_fields(raw, index)
    tid = raw.get("id")
    if not isinstance(tid, str) or not _TOPIC_ID_RE.match(tid):
        raise ValueError("topic id out of range")
    topic = Topic(id=tid, dry_run=_stored_dry_run(raw.get("dry_run")), **fields)
    # Belt: a topic is on only with a breadth test of its current wording that
    # didn't match too much — whatever wrote the record, anything else (no
    # test, a test of other wording, a too-broad one) reads as off.
    if topic.enabled and (not _tested_now(topic) or _too_broad(topic.dry_run)):
        topic = replace(topic, enabled=False)
    return topic


def parse_config(data: Any) -> AutoReplyConfig:
    """A stored record as a config. Raises ValueError when anything in it is
    unreadable or out of range (the lint and the breadth gate are save-time
    rules and aren't re-applied; a topic that is on without a breadth test of
    its current wording, or with one that matched too much, reads as off)."""
    if not isinstance(data, Mapping):
        raise ValueError("not an object")
    topics_raw = data.get("topics")
    if topics_raw is None:
        topics_raw = []
    if not isinstance(topics_raw, list) or len(topics_raw) > MAX_TOPICS:
        raise ValueError("topics out of range")
    topics = tuple(_stored_topic(t, i) for i, t in enumerate(topics_raw))
    if len({t.id for t in topics}) != len(topics):
        raise ValueError("duplicate topic id")
    if len([t for t in topics if t.enabled]) > MAX_ENABLED_TOPICS:
        raise ValueError("too many topics on")
    return AutoReplyConfig(
        enabled=_bool(data.get("enabled"), "enabled", False),
        agent_ref=_agent_ref(data.get("agent_ref")),
        daily_limit=_daily_limit(data.get("daily_limit")),
        never_disclose=_never_disclose(data.get("never_disclose")),
        topics=topics,
        google_sub=_managed_str(data.get("google_sub"), "google_sub"),
        account_email=_managed_str(data.get("account_email"), "account_email"),
        enabled_at=_managed_str(data.get("enabled_at"), "enabled_at"),
        disabled_reason=_managed_str(data.get("disabled_reason"), "disabled_reason"),
        updated_at=_managed_str(data.get("updated_at"), "updated_at"),
    )


async def load_autoreply_config(db: Any, user_id: str) -> AutoReplyConfig:
    """*user_id*'s config — the defaults (auto-replies OFF) when none is stored
    or the record is unreadable or out of range anywhere: a corrupt record never
    turns auto-replies on. A failing database read is raised, not read as off."""
    raw = await db.get_setting(user_id, AUTOREPLY_KEY)
    if not raw:
        return default_config()
    try:
        return parse_config(json.loads(raw))
    except Exception as exc:
        log.warning("Unreadable Gmail auto-reply config for a user; using the defaults (off): %s",
                    type(exc).__name__)
        return default_config()


async def save_autoreply_config(db: Any, user_id: str, cfg: AutoReplyConfig, *,
                                now: datetime | None = None) -> AutoReplyConfig:
    """Store *cfg* (stamped ``updated_at``) and return what was stored, as the
    loader reads it back (so a topic the loader would read as off is off in
    the answer too). The caller holds the read-modify-write lock. Raises
    ValueError, writing nothing, when the record wouldn't read back."""
    data = replace(cfg, updated_at=_iso(now)).to_json()
    stored = parse_config(data)
    await db.set_settings(user_id, {AUTOREPLY_KEY: json.dumps(data, ensure_ascii=True)})
    return stored


# ── a PUT ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PatchResult:
    """What :func:`apply_patch` made of an update. ``config`` is never more on
    than before: ``wants_enable`` says the user asked to turn auto-replies on
    (the route checks every precondition, then calls :func:`enable`).
    ``auto_disabled`` lists topics turned off because a matching field
    changed; ``notice`` is then :data:`RERUN_NOTICE`."""

    config: AutoReplyConfig
    wants_enable: bool = False
    auto_disabled: tuple[str, ...] = ()
    notice: str = ""


def _new_topic_id(taken: set[str]) -> str:
    while True:
        tid = "t_" + secrets.token_hex(4)
        if tid not in taken:
            taken.add(tid)
            return tid


def _patch_topics(raw: Any, current: AutoReplyConfig,
                  now: datetime | None) -> tuple[tuple[Topic, ...], tuple[str, ...]]:
    if not isinstance(raw, list):
        raise ConfigError("topics must be a list")
    if len(raw) > MAX_TOPICS:
        raise ConfigError(f"There can be at most {MAX_TOPICS} topics (got {len(raw)})")
    # An id is the server's: a known one edits that topic, any other is a new
    # topic with a fresh id (never one a deleted topic had).
    taken = {t.id for t in current.topics}
    kept: set[str] = set()
    topics: list[Topic] = []
    auto_disabled: list[str] = []
    switched_on: list[Topic] = []
    for i, item in enumerate(raw):
        fields = _topic_fields(item, i)
        label = _label(item, i)
        reason = lint_topic(fields["name"], fields["about"], fields["examples"])
        if reason:
            raise ConfigError(f"{label}: {reason}")
        tid = item.get("id")
        old = current.topic(tid) if isinstance(tid, str) and tid else None
        if old is not None:
            if old.id in kept:
                raise ConfigError(f"{label}: the same topic is listed twice")
            kept.add(old.id)
            topic = Topic(id=old.id, dry_run=old.dry_run, **fields)
        else:
            topic = Topic(id=_new_topic_id(taken), **fields)
        was_on = old is not None and old.enabled
        if was_on and topic.match_hash != old.match_hash:
            # What it matches changed: off until a breadth test of the new wording.
            if topic.enabled:
                auto_disabled.append(topic.id)
            topic = replace(topic, enabled=False)
        elif topic.enabled and not was_on:
            switched_on.append(topic)
        topics.append(topic)
    on = [t for t in topics if t.enabled]
    if len(on) > MAX_ENABLED_TOPICS:
        raise ConfigError(
            f"At most {MAX_ENABLED_TOPICS} topics can be on at once (got {len(on)})")
    for topic in switched_on:
        gate = dry_run_gate(topic, now=now)
        if not gate.ok:
            raise ConfigError(f'Topic "{topic.name}" can\'t be turned on: {gate.reason}',
                              status=409)
    return tuple(topics), tuple(auto_disabled)


def apply_patch(current: AutoReplyConfig, patch: Any, *,
                now: datetime | None = None) -> PatchResult:
    """*current* with a PUT's partial update applied — ``enabled``,
    ``agent_ref``, ``daily_limit``, ``never_disclose`` and ``topics`` (the whole
    list); an absent or null field is left unchanged, server-managed fields and
    every topic's ``dry_run`` are ignored. Raises :class:`ConfigError`."""
    if not isinstance(patch, Mapping):
        raise ConfigError("The update must be a JSON object")
    changes: dict[str, Any] = {}
    wants_enable = False
    if patch.get("enabled") is not None:
        on = _bool(patch["enabled"], "enabled", False)
        if on and not current.enabled:
            wants_enable = True
        elif not on and current.enabled:
            changes.update(enabled=False, disabled_reason="")
    if patch.get("agent_ref") is not None:
        changes["agent_ref"] = _agent_ref(patch["agent_ref"])
    if patch.get("daily_limit") is not None:
        changes["daily_limit"] = _daily_limit(patch["daily_limit"])
    if patch.get("never_disclose") is not None:
        changes["never_disclose"] = _never_disclose(patch["never_disclose"])
    auto_disabled: tuple[str, ...] = ()
    if patch.get("topics") is not None:
        changes["topics"], auto_disabled = _patch_topics(patch["topics"], current, now)
    return PatchResult(
        config=replace(current, **changes),
        wants_enable=wants_enable,
        auto_disabled=auto_disabled,
        notice=RERUN_NOTICE if auto_disabled else "",
    )


# ── server-side changes ─────────────────────────────────────────────


def enable_problems(cfg: AutoReplyConfig, *, now: datetime | None = None) -> list[str]:
    """What, in the config itself, stands in the way of turning auto-replies on
    (the route adds the deck, Google, signature, agent and model checks)."""
    problems: list[str] = []
    if not cfg.agent_ref:
        problems.append("choose the agent that writes the replies")
    if not cfg.enabled_topics:
        problems.append("turn on at least one topic")
    for topic in cfg.enabled_topics:
        gate = dry_run_gate(topic, now=now)
        if not gate.ok:
            problems.append(f'topic "{topic.name}": {gate.reason}')
    return problems


def enable(cfg: AutoReplyConfig, *, google_sub: str, account_email: str,
           now: datetime | None = None) -> AutoReplyConfig:
    """*cfg* switched on and bound to the Google account it answers from."""
    return replace(cfg, enabled=True, google_sub=google_sub, account_email=account_email,
                   enabled_at=_iso(now), disabled_reason="")


def disable(cfg: AutoReplyConfig, reason: str = "") -> AutoReplyConfig:
    """*cfg* switched off; *reason* (``paused_by_user``, ``auto_paused``,
    ``google_disconnected``, ``google_account_changed`` …) is shown to the user."""
    return replace(cfg, enabled=False, disabled_reason=reason)


def disable_topic(cfg: AutoReplyConfig, topic_id: str) -> AutoReplyConfig:
    """*cfg* with one topic switched off (the drift breaker); unchanged when
    there is no such topic. Its breadth test is dropped too: it was measured
    before the topic was seen matching most of the mail, so switching it on
    again needs a new one — a page still showing it on can't send it back on
    (the gate refuses), and nothing can bring the old test back."""
    return replace(cfg, topics=tuple(replace(t, enabled=False, dry_run=DryRun())
                                     if t.id == topic_id else t
                                     for t in cfg.topics))


def record_dry_run(cfg: AutoReplyConfig, topic_id: str, *, match_hash: str, eligible: int,
                   matched: int, now: datetime | None = None) -> AutoReplyConfig:
    """*cfg* with a breadth test stored on its topic. *match_hash* is the
    topic's hash when the test started: if the topic was edited meanwhile, the
    stored test simply doesn't open the gate — and it never replaces a test of
    the topic's current wording (that one is kept). A test of the current
    wording that matched too much switches a topic that is on off (its rate
    kept on show). Raises :class:`ConfigError` (404) when the topic is gone."""
    topic = cfg.topic(topic_id)
    if topic is None:
        raise ConfigError("No such topic", status=404)
    eligible = _count(eligible, "eligible")
    matched = _count(matched, "matched")
    if matched > eligible or not _HASH_RE.match(match_hash or ""):
        raise ValueError("dry run out of range")
    if match_hash != topic.match_hash and _tested_now(topic):
        return cfg
    # A re-test of the same wording keeps when it was first tested: the drift
    # breaker samples from then, so re-testing can't wipe its evidence.
    prior = topic.dry_run
    at = _iso(now)
    since = (prior.since or prior.at) if prior.match_hash == match_hash and prior.at else at
    dry = DryRun(match_hash=match_hash, at=at, eligible=eligible, matched=matched,
                 rate=round(matched / eligible, 4) if eligible else 0.0, since=since)
    tested = replace(topic, dry_run=dry)
    if tested.enabled and (not _tested_now(tested) or _too_broad(dry)):
        tested = replace(tested, enabled=False)
    return replace(cfg, topics=tuple(tested if t.id == topic_id else t for t in cfg.topics))
