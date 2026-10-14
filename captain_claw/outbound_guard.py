"""Layer 1 of the topic auto-reply's sensitive-data guard: a deterministic
scan of the reply the writer model proposed, before anything else sees it.

Layer 0 is what the writer is shown at all: the topic brief, scanned folder
excerpts, category-restricted background, the thread the sender already saw
and the sender's own email. Layer 2 is the semantic judge, which runs only
when this scan found nothing, so suspected data never goes to a third party.

Every detector has a level:

* **block**: the reply is never sent and its text is not stored. Secrets
  (key prefixes, JWTs, private keys, ``user:pass@`` links, token query
  parameters, recovery phrases, "password: …" in English, Croatian and
  German); high-entropy runs, unless the exact run is in a SHAREABLE source
  (a form link in the brief); Luhn-valid card numbers; internal hosts and
  local paths; the writer prompt's canary or a 12-word run of any Flight Deck
  prompt; an 8-word run of the owner's private profile; the owner's
  never-disclose terms, inflected (EN/HR/DE endings), joined or spaced out.
* **hold**: the reply waits for the owner. IBANs (mod-97), OIBs (ISO 7064
  MOD 11,10), JMBGs, SSN shapes and other runs of 9+ digits, email addresses,
  phone numbers, amounts, links (bare domains too: mail clients link them),
  HTML and markdown images. Each passes when it has provenance: it is in a
  SHAREABLE source (brief, folder excerpts, own addresses, signature) or in
  the inbound mail, so the sender may hear their own order number back; but
  a mail that lists more than a few values of a kind vouches for none of
  them. Links are stricter. The inbound mail never vouches for one; a
  SHAREABLE source must hold the link itself or link a parent folder of it
  (the site root included), or the link must have no query or fragment and
  be on the owner's own mail domain or on a site a SHAREABLE source links
  whole.

Background (insights, playbooks, web results) is never provenance, and a
value it holds needs a SHAREABLE source even when the sender wrote it too.

Every scan reads the text normalised: NFKC; invisible characters (format
characters, combining marks, variation selectors, fillers) dropped; digits
as ASCII; a run of spaces as one. Digit groups split by marks or line breaks
are read whole where they validate as a card, phone, OIB, JMBG or IBAN.

A :class:`Finding` carries the detector kind, its level, a masked hint (the
kind plus at most the first and last two characters, nothing at all for
secrets) and ``echo``: whether the value was already in the inbound mail, so
a reply that only repeats the sender's own token doesn't count towards
auto-pause. Raw values never leave this module.

:func:`scan_reply` is the full scan, :func:`scan_material` the knowledge
pre-scan (block classes plus IDs; a chunk with any finding is dropped), and
:func:`scan_blocks` re-runs only the block classes on a text the owner
approved or edited, since for hold classes the owner is the judge.

Pure and standard library only. Nothing here raises: an internal error is a
``guard_error`` block finding.
"""

from __future__ import annotations

import functools
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

BLOCK = "block"
HOLD = "hold"
PASS = "pass"

GUARD_ERROR = "guard_error"

# Blocked, and never given a hint: even two characters of these help nobody.
SECRET_KINDS = frozenset({
    "secret_key", "jwt", "private_key", "url_credentials", "token_param",
    "seed_phrase", "credential_phrase", "high_entropy", "canary", "prompt_echo",
    "private_copy", GUARD_ERROR,
})
BLOCK_KINDS = SECRET_KINDS | frozenset({
    "card_number", "internal_host", "local_path", "never_disclose",
})
ID_KINDS = frozenset({"iban", "oib", "jmbg", "ssn", "long_number"})
HOLD_KINDS = ID_KINDS | frozenset({
    "email_address", "phone_number", "amount", "url", "html", "markdown_image",
})

# What a bell or card calls each kind ("It would have contained …").
KIND_LABELS = {
    "secret_key": "an API key or token",
    "jwt": "a login token",
    "private_key": "a private key",
    "url_credentials": "a link with a password in it",
    "token_param": "a link carrying an access token",
    "seed_phrase": "a recovery phrase",
    "credential_phrase": "a password or PIN",
    "high_entropy": "a secret-looking code",
    "card_number": "a card number",
    "internal_host": "an internal network address",
    "local_path": "a file path",
    "canary": "its own instructions",
    "prompt_echo": "its own instructions",
    "private_copy": "text from your private profile",
    "never_disclose": "one of your never-mention terms",
    GUARD_ERROR: "something the safety check couldn't read",
    "iban": "a bank account number",
    "oib": "an OIB",
    "jmbg": "a JMBG",
    "ssn": "a social security number",
    "long_number": "a long number",
    "email_address": "an email address",
    "phone_number": "a phone number",
    "amount": "an amount",
    "url": "a link",
    "html": "HTML",
    "markdown_image": "an image",
}

# A text this long is not a reply; scanning it would only be slow.
MAX_SCAN_CHARS = 200_000

# Hints show the first and last two characters only of values this long.
_HINT_MIN_CHARS = 8
_PROMPT_SHINGLE = 12
_PRIVATE_SHINGLE = 8
# A shingle counts only with this many content words in it, so "I would be
# happy to help you with" in the owner's profile doesn't block every reply.
_SHINGLE_CONTENT_WORDS = 3


@dataclass(frozen=True)
class Finding:
    """One detector hit. ``hint`` is masked (see :func:`_hint`); ``echo`` says
    the value was already in the inbound mail."""

    kind: str
    level: str
    hint: str
    echo: bool


@dataclass(frozen=True)
class Verdict:
    """Every finding of one scan, blocks first, then in text order."""

    findings: tuple[Finding, ...] = ()

    @property
    def level(self) -> str:
        """``block`` if any finding blocks, ``hold`` if any holds, else ``pass``."""
        levels = {f.level for f in self.findings}
        if BLOCK in levels:
            return BLOCK
        return HOLD if HOLD in levels else PASS

    @property
    def kinds(self) -> tuple[str, ...]:
        """The distinct finding kinds, in finding order."""
        return tuple(dict.fromkeys(f.kind for f in self.findings))

    @property
    def echo_only(self) -> bool:
        """A block whose every block finding repeats the inbound mail: the
        sender asked us to say their own data back. Blocked all the same, but
        not evidence that the writer leaked anything."""
        blocks = [f for f in self.findings if f.level == BLOCK]
        return bool(blocks) and all(f.echo for f in blocks)

    def as_dicts(self) -> list[dict[str, Any]]:
        """The findings as JSON-ready dicts (already masked)."""
        return [asdict(f) for f in self.findings]


_ERROR_VERDICT = Verdict((Finding(GUARD_ERROR, BLOCK, "", False),))


def describe_kinds(kinds: Iterable[str]) -> str:
    """"a card number and a file path": the labels of *kinds* for a bell,
    each once, in order."""
    labels: list[str] = []
    for kind in kinds:
        label = KIND_LABELS.get(kind, "sensitive data")
        if label not in labels:
            labels.append(label)
    if not labels:
        return "sensitive data"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


# ── Normalisation ────────────────────────────────────────────────────

_DASHES = {ord(c): "-" for c in "‐‑‒–—―−﹘﹣－"}
# Letters NFKD leaves whole.
_FOLD_EXTRA = {"đ": "d", "Đ": "d", "ł": "l", "Ł": "l", "ø": "o", "Ø": "o",
               "ħ": "h", "Ħ": "h", "ı": "i", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe"}
_WORD_RE = re.compile(r"[^\W_]+")
_BLANK = "\x00"

# Characters a mail client draws as nothing (or as blank space) that would
# still split a word, a digit run or a link for every detector, so the
# recipient reads "Orion" or a card number whole while a scan sees pieces.
# Format characters (Cf) and combining marks (Mn, Me: variation selectors,
# the grapheme joiner, accents NFKC couldn't compose) go by category; these
# are the rest of Unicode's Default_Ignorable_Code_Point set plus the
# blank-drawing fillers.
_DROPPED_CATEGORIES = frozenset({"Cf", "Mn", "Me"})
_INVISIBLE_RANGES = (
    (0x115F, 0x1160),       # Hangul choseong and jungseong fillers
    (0x17B4, 0x17B5),       # Khmer inherent vowels
    (0x180B, 0x180F),       # Mongolian variation selectors, vowel separator
    (0x2065, 0x2065),       # reserved, default-ignorable
    (0x2800, 0x2800),       # braille pattern blank
    (0x3164, 0x3164),       # Hangul filler
    (0xFE00, 0xFE0F),       # variation selectors
    (0xFFA0, 0xFFA0),       # halfwidth Hangul filler
    (0xFFF0, 0xFFF8),       # reserved, default-ignorable
    (0x1BCA0, 0x1BCA3),     # shorthand format controls
    (0x1D159, 0x1D159),     # musical symbol null notehead
    (0x1D173, 0x1D17A),     # musical beam and phrase controls
    (0xE0000, 0xE0FFF),     # tags, variation selectors supplement, reserved
)
_INVISIBLE = frozenset(chr(c) for lo, hi in _INVISIBLE_RANGES for c in range(lo, hi + 1))
_SPACE_RUN_RE = re.compile(r" {2,}")


def _prep(text: str) -> str:
    """*text* as the detectors read it: NFKC (fullwidth digits and letters
    become ASCII); format characters (zero-width, bidi), combining marks and
    the other invisible characters in :data:`_INVISIBLE` dropped; every
    decimal digit as its ASCII digit; Unicode dashes as ``-``; other spaces
    and tabs as a space, and a run of them as one."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", text.replace("\r\n", "\n")):
        if ch == "\n":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in _DROPPED_CATEGORIES or ch in _INVISIBLE:
            continue
        if cat == "Nd":
            out.append(str(unicodedata.decimal(ch, 0)))
        elif cat in ("Zl", "Zp") or ch == "\r":
            out.append("\n")
        elif cat in ("Zs", "Cc"):
            out.append(" ")
        else:
            out.append(ch)
    return _SPACE_RUN_RE.sub(" ", "".join(out).translate(_DASHES))


@functools.lru_cache(maxsize=8192)
def _fold_char(ch: str) -> str:
    if ch in _FOLD_EXTRA:
        return _FOLD_EXTRA[ch]
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return base.casefold()


def _fold(text: str) -> str:
    """Casefolded with diacritics dropped (``Šifra`` → ``sifra``, ``Đ`` → ``d``)."""
    return "".join(map(_fold_char, text))


def _fold_same_len(text: str) -> str:
    """:func:`_fold`, one character for one, so match offsets stay valid."""
    out: list[str] = []
    for ch in text:
        folded = _fold_char(ch)
        if len(folded) == 1:
            out.append(folded)
        else:
            lower = ch.lower()
            out.append(lower if len(lower) == 1 else ch)
    return "".join(out)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(_fold(text))


def _squash(text: str) -> str:
    return " ".join(text.split())


def _digits(text: str) -> str:
    return "".join(c for c in text if "0" <= c <= "9")


def _compact(text: str) -> str:
    return "".join(c for c in _fold(text) if c.isascii() and c.isalnum())


def _hint(kind: str, value: str) -> str:
    """``iban: HR…89``: the kind plus the first and last two characters of a
    value of 8 or more; the kind alone for a shorter one; nothing for a
    secret."""
    if kind in SECRET_KINDS:
        return ""
    v = "".join(str(value or "").split())
    if len(v) >= _HINT_MIN_CHARS:
        return f"{kind}: {v[:2]}…{v[-2:]}"
    return kind


def _texts(value: Any) -> list[str]:
    """A sequence of texts as a list of non-empty strings (a lone string is
    one text, not its characters)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [v if isinstance(v, str) else str(v) for v in value if v]


# ── Never-disclose matching ──────────────────────────────────────────

# Endings a term word takes and is still the term: English and German plurals
# and verb forms; Croatian cases, possessives and adjectives ("Orion" →
# "Oriona", "Orionu", "Orionom", "Orionov", "orionski").
_ND_SUFFIXES = frozenset("""
s es ed ing er ers en ern ens
a e i o u om em ima ama oj og oga omu ome im ih ju ji je ja jom jem jima
ov ova ove ovi ovu ovo ovom ovoj ovog ovih ovim ovima
ev eva eve evi evu evo evom evoj evog evih evim evima
in ina ine ini inu ino inom inoj inog inih inim inima
ski ska ske sko sku skog skom skoj skih skim skima
""".split())
# A short term ("Max", "Fin") takes only endings that don't turn it into
# another common word ("fine", "bare", "even").
_ND_SHORT_SUFFIXES = frozenset("s es a i u om em ov ova ove ovi ovu ovo ovom".split())
# A short vowel-final name inflects on its stem: Luna → Lune, Lunu, Luni,
# Lunom; Ana → Ani, Anu, Anin, Anina; Luka → Lukin.
_ND_SHORT_STEM_SUFFIXES = frozenset(
    "a e i o u om oj in ina ine ini inu ino inom inoj inog inih inim".split())
# A stem made by dropping a vowel ("Marko" → "mark") takes only Croatian
# endings: English plurals and verb endings would make "marks", "marked".
_ND_STEM_SUFFIXES = None   # set below, from _ND_SUFFIXES
# Stems ("Rijeka" → "Rijeku", "Rijeci"; "Petar" → "Petra") from this length.
_ND_STEM_MIN = 5
_ND_PALATAL = {"k": "c", "g": "z", "h": "s"}
# Joined forms ("ProjectOrion", "Pro ject Orion") are compared compacted, from
# a word start, for terms this long: a one-word term needs more, since
# "falconry" is not "falcon".
_ND_COMPACT_MIN_MULTI = 6
_ND_COMPACT_MIN_SINGLE = 8
_ND_LETTER_RUN = 3
_ND_STEM_SUFFIXES = _ND_SUFFIXES - frozenset("s es ed ing er ers en ern ens".split())
# A capitalised compound starting with the term ("Orionprojekt",
# "Atlasplattform"): terms of this many letters, followed by this many more.
_ND_PREFIX_MIN = 4
_ND_PREFIX_REST = 3


def _nd_variants(word: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(free, proper)``: the text words that count as the folded term word
    *word*. *free* — the word and its regular endings ("Oriona", "companies")
    — match in any case. *proper* — forms built on a stem or a short word
    ("Lunu", "Timu", "Rijeci", "Petra") — match only a capitalised text word
    when the owner wrote the term capitalised: a proper noun keeps its
    capital when inflected, the common words those forms collide with
    ("timu", "riječi", "marka") don't."""
    free = {word}
    proper: set[str] = set()
    if len(word) < 3 or not word.isalpha():
        return frozenset(free), frozenset()
    if len(word) < _ND_STEM_MIN:
        proper.update(word + s for s in _ND_SHORT_SUFFIXES)
        if word[-1] in "aeiou":
            proper.update(word[:-1] + s for s in _ND_SHORT_STEM_SUFFIXES)
        return frozenset(free), frozenset(proper - free)
    free.update(word + s for s in _ND_SUFFIXES)
    stems: list[str] = []
    if word[-1] in "aeiou":
        stem = word[:-1]
        stems.append(stem)
        if stem[-1] in _ND_PALATAL:             # Rijeka → Rijeci, knjiga → knjizi
            proper.update(stem[:-1] + _ND_PALATAL[stem[-1]] + s for s in ("i", "ima"))
    elif word[-2] == "a" and word[-3] not in "aeiou":
        stems.append(word[:-2] + word[-1])      # Petar → Petra, Zadar → Zadru
    if word[-1] == "y":
        free.update(word[:-1] + s for s in ("ies", "ied"))
    for stem in stems:
        proper.update(stem + s for s in _ND_STEM_SUFFIXES)
    return frozenset(free), frozenset(proper - free)


def _tokens(text: str) -> list[tuple[str, bool]]:
    """*text*'s words, folded, each with whether it was written capitalised."""
    return [(w, tok[:1].isupper()) for tok in _WORD_RE.findall(text) for w in _words(tok)]


@functools.lru_cache(maxsize=1024)
def _term_shape(term: str) -> tuple | None:
    """``(folded words, each word's (free, proper, capitalised) variants, the
    compact form to look for from a word start or "", the digits of an
    all-digit term of 6+ or "", the compound prefix or "")`` for one
    never-disclose term; None for a term with no words."""
    p = _prep(term)
    toks = _tokens(p)
    if not toks:
        return None
    words = [w for w, _c in toks]
    compact = _compact(p)
    min_len = _ND_COMPACT_MIN_MULTI if len(words) > 1 else _ND_COMPACT_MIN_SINGLE
    digits = _digits(p)
    if len(digits) < 6 or any(c.isalpha() for c in p):
        digits = ""
    variants = tuple((*_nd_variants(w), cap) for w, cap in toks)
    prefix = (words[0] if len(toks) == 1 and toks[0][1] and words[0].isalpha()
              and len(words[0]) >= _ND_PREFIX_MIN else "")
    return (" ".join(words), variants,
            compact if len(compact) >= min_len else "", digits, prefix)


def _split_joins(token: str) -> list[str]:
    """``ProjectOrion`` → ``Project``, ``Orion``; ``Orion2026`` → ``Orion``,
    ``2026``."""
    pieces: list[str] = []
    cur = ""
    for ch in token:
        if cur and ((cur[-1].islower() and ch.isupper()) or cur[-1].isdigit() != ch.isdigit()):
            pieces.append(cur)
            cur = ""
        cur += ch
    if cur:
        pieces.append(cur)
    return pieces


def _merge_letter_runs(tokens: Sequence[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Runs of 3+ one-letter words as one word (``O r i o n`` → ``orion``,
    capitalised as its first letter was)."""
    out: list[tuple[str, bool]] = []
    run: list[tuple[str, bool]] = []
    for tok in [*tokens, ("", False)]:
        w = tok[0]
        if len(w) == 1 and w.isalpha():
            run.append(tok)
            continue
        if len(run) >= _ND_LETTER_RUN:
            out.append(("".join(x for x, _c in run), run[0][1]))
        else:
            out.extend(run)
        run.clear()
        if w:
            out.append(tok)
    return out


class _TermText:
    """A prepared text as the never-disclose matcher reads it: its folded
    words; the same split at camel-case and letter/digit joins; with runs of
    single letters merged; and compacted, with the offsets where words start."""

    def __init__(self, prepped: str):
        toks = _tokens(prepped)
        words = [w for w, _c in toks]
        self.sequences = [toks]
        split = [(w, piece[:1].isupper()) for tok in _WORD_RE.findall(prepped)
                 for piece in _split_joins(tok) for w in _words(piece)]
        merged = _merge_letter_runs(toks)
        for seq in (split, merged):
            if seq != toks:
                self.sequences.append(seq)
        self.vocab = {w for seq in self.sequences for w, _c in seq}
        parts: list[str] = []
        self.starts: set[int] = set()
        pos = 0
        for w in words:
            c = "".join(ch for ch in w if ch.isascii() and ch.isalnum())
            if c:
                self.starts.add(pos)
                parts.append(c)
                pos += len(c)
        self.compact = "".join(parts)

    def has(self, shape: tuple) -> bool:
        """Whether a term of this :func:`_term_shape` is in the text, in any
        of its inflected, joined or spaced-out forms."""
        variants, compact, prefix = shape[1], shape[2], shape[4]
        if compact:
            pos = self.compact.find(compact)
            while pos != -1:
                if pos in self.starts:
                    return True
                pos = self.compact.find(compact, pos + 1)
        if prefix:                              # "Orionprojekt", "Atlasplattform"
            for seq in self.sequences:
                for w, cap in seq:
                    if (cap and w.startswith(prefix) and len(w) >= len(prefix) + _ND_PREFIX_REST
                            and w[len(prefix):].isalpha()):
                        return True
        free0, proper0, _cap0 = variants[0]
        if not (free0 | proper0) & self.vocab:
            return False

        def fits(tok: tuple[str, bool], v: tuple[frozenset[str], frozenset[str], bool]) -> bool:
            w, cap = tok
            return w in v[0] or (w in v[1] and (cap or not v[2]))

        n = len(variants)
        for seq in self.sequences:
            for i in range(len(seq) - n + 1):
                if all(fits(seq[i + j], variants[j]) for j in range(n)):
                    return True
        return False


# ── Checksums ────────────────────────────────────────────────────────


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    """IBAN mod-97: the first four characters moved to the end, letters as
    10..35, the whole number ≡ 1 (mod 97)."""
    s = value.upper()
    if not (15 <= len(s) <= 34 and s.isascii() and s.isalnum()
            and s[:2].isalpha() and s[2:4].isdigit()):
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _oib_ok(digits: str) -> bool:
    """Croatian OIB: 11 digits, the last an ISO 7064 MOD 11,10 check digit."""
    if len(digits) != 11 or not digits.isdigit():
        return False
    a = 10
    for ch in digits[:10]:
        a = (a + int(ch)) % 10 or 10
        a = (a * 2) % 11
    return (11 - a) % 10 == int(digits[10])


def _jmbg_ok(digits: str) -> bool:
    """JMBG: 13 digits, the last ``11 - Σ wᵢ·dᵢ mod 11`` with weights
    7..2 on each half (a result over 9 is 0)."""
    if len(digits) != 13 or not digits.isdigit():
        return False
    x = [int(c) for c in digits]
    s = sum(w * (x[i] + x[i + 6]) for i, w in enumerate((7, 6, 5, 4, 3, 2)))
    k = 11 - s % 11
    return (0 if k > 9 else k) == x[12]


# ── Detectors: patterns ──────────────────────────────────────────────

_TOKEN_RES = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),           # sk-, sk-ant-, sk-or-, sk-proj-
    re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Za-z])"),
    re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_-])xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"(?<![A-Za-z0-9_-])ya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9/])1//0[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])cc_pat_[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{20,}"),
)
_JWT_RE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?")
_PEM_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----(?:.*?-----END[A-Z0-9 ]*-----)?", re.S)
_URL_CREDENTIALS_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{0,30}://[^\s/?#@:]+:[^\s/?#@]+@[^\s/?#]*")
_TOKEN_PARAM_RE = re.compile(
    r"(?i)[?&#;](access_token|refresh_token|id_token|token|api_key|apikey|key|client_secret"
    r"|secret|sig|signature|password|passwd|pwd|fd_token|auth|t)=([^&#\s\"'<>]+)")

# A BIP39-shaped recovery phrase: 12+ lowercase words of 3–8 letters in a row,
# nothing but spaces (or "1." numbering) between them. Prose of that length
# always has a short word, a long word, a capital, punctuation or one of these.
_SEED_MIN_WORDS = 12
_SEED_WORD_RE = re.compile(r"[a-z]{3,8}")
_SEED_NUMBERING_RE = re.compile(r"^\d{1,2}[.):]")
_SEED_STOPWORDS = frozenset("""
the and for you your with have from are was were not but our has had she its been what which
who how why into would could should der die das und ist ein eine nicht mit von den dem des sie
wir ich kao ili koji koja koje ali sam smo ste bio bila bilo
""".split())

# "Password: …", "PIN is 4821", "lozinka je …", "Passwort lautet …", matched
# on folded text (šifra → sifra, Passwört → passwort).
_CRED_LABELS = (
    r"passwords?|passwd|passcodes?|pass[ \t]?codes?|passphrases?|pass[ \t]phrases?"
    r"|pins?(?:[ \t-]?(?:code|kod|broj|nummer|number))?"
    r"|otp|one[ \t-]time[ \t]+(?:code|password|pin)|(?:recovery|backup|security)[ \t]+codes?"
    r"|cvv2?|cvc2?|lozink[aeiou]m?|sifr[aeiou]m?|zapork[aeiou]m?|passwort(?:e|er)?|passworter"
    r"|kennwort(?:e|er)?|zugangscode|wiederherstellungscode"
    # keys and tokens
    r"|(?:(?:access|auth|api|bearer|refresh|session|secret|personal[ \t]+access)[ \t_-]?)?tokens?"
    r"|api[ \t_-]?keys?|(?:secret|private|access|license|licence)[ \t_-]?keys?"
    r"|(?:client|app|application|api|shared)[ \t_-]?secrets?"
    # a door, an alarm, a network: codes that open something ("Discount code"
    # and "promo code" are not here — a brief shares those on purpose)
    r"|(?:access|door|gate|alarm|entry|entrance|wi-?fi|wlan|network|lock|safe|garage|keypad"
    r"|building|lift|elevator|router|guest[ \t]+wi-?fi)[ \t-]?(?:codes?|keys?|pins?|passwords?)"
    r"|kod[ \t]+(?:za[ \t]+)?(?:vrata|alarm\w*|ulaz\w*|wi-?fi|mrez\w*|sef\w*|garaz\w*)"
    r"|лозинк\w*|шифр\w*|парол\w*"
)
_CRED_RE = re.compile(
    rf"\b(?:{_CRED_LABELS})\b"
    # up to two words in between: "šifra alarma je", "Wi-Fi key for guests is"
    r"(?:[ \t]+[^\W\d_]{1,15}){0,2}?"
    r"(?P<sep>[ \t]*[:=][ \t]*|[ \t]+(?:is|je|ist|glasi|lautet)[ \t]*:?[ \t]*)"
    r"(?P<q>[\"'`“”„‘’«»])?(?P<val>[^\s\"'`“”„‘’«»]{3,})")
# Short labels count only right before ":" or "=" ("pwd: …", "pw=…"): "the
# pass is valid" and "pin the date" stay prose.
_CRED_TERSE_RE = re.compile(
    r"\b(?:pass|pw|pwd|pwrd|secret|token|key)\b"
    r"(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?P<q>[\"'`“”„‘’«»])?(?P<val>[^\s\"'`“”„‘’«»]{3,})")
_CRED_TRAILING = ".,;:!?)]}"
# Words that follow "password is" / "Password:" in prose, never a password.
_CRED_SAFE = frozenset("""
the your our their his her its this that these those any some one not now still also only just
required needed mandatory optional necessary valid invalid expired incorrect wrong correct right
same different reset sent changed updated generated created protected secure strong weak secret
private personal confidential temporary hidden shown displayed stored saved encrypted attached
included enclosed provided below above missing empty blank unknown available unavailable active
inactive locked blocked disabled enabled case-sensitive sensitive important ready being never
always usually here there please none
obavezna obavezno obvezna potrebna potrebno ispravna neispravna pogresna kriva tocna istekla
promijenjena poslana poslan ista isti vasa tvoja moja nasa osjetljiva sigurna tajna privremena
skrivena dolje gore ispod iznad nije jos uvijek sada
erforderlich notwendig falsch richtig korrekt abgelaufen ungultig gultig ihr ihre dein deine
mein meine unser unsere sicher geheim vertraulich unten oben nicht noch jetzt bereits schon
immer leer gesperrt
""".split())

_ENTROPY_RUN_RE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
# A hex key or token of 32+ ("4c5e1b7a…", a 40-hex API key): two character
# classes only, so the entropy rule above never sees it.
_HEX_RUN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
_ENTROPY_MIN_CHARS = 32
_ENTROPY_MIN_BITS = 4.0
_SLUG_SPLIT_RE = re.compile(r"[-_/.+=]")

_IBAN_START_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{2}[0-9]{2}(?=[ \t\n.,/_·•|:;*~'()-]{0,4}[A-Za-z0-9])")
_IBAN_MAX = 34
_IBAN_MIN = 15

# Digits that are nobody's: dates, year ranges, IP addresses (private ones
# block on their own) — real ones, every octet 0–255: "694.351.515.30" is an
# OIB written with dots, not an address.
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_NOT_NUMBER_RE = re.compile(
    r"(?<![0-9.])(?:"
    r"(?:0?[1-9]|[12][0-9]|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:[0-9]{4}|[0-9]{2})"
    r"|(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12][0-9]|3[01])/(?:[0-9]{4}|[0-9]{2})"
    r"|[0-9]{4}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12][0-9]|3[01])"
    r"|(?:19|20)[0-9]{2}[-/](?:19|20)[0-9]{2}"
    rf"|{_OCTET}(?:\.{_OCTET}){{3}}"
    r")(?![0-9]|\.[0-9])")

# A run of digits with single separators between them: "+385 (0)1 234-5678",
# "4111 1111 1111 1111", "091/234.5678". Spaced dashes (ranges) end a run.
_NUM_SEP = r"(?:[ \t]?\([ \t]?|\)[ \t]?|[-./]|[ \t])"
_NUM_RUN_RE = re.compile(rf"(?<![0-9])(?:\+[ \t]?|\(\+?)?[0-9](?:{_NUM_SEP}?[0-9])*")
# What a run above doesn't join, but an instructed writer or a hard-wrapped
# file puts between digit groups: a mark with spaces around it ("4111 - 1111",
# "4111, 1111"), an underscore, a line break. Such a stretch counts only where
# its separators are all one kind and its digits validate (a card's print
# groups, issuer prefix and Luhn; a phone's shape; an OIB or JMBG checksum),
# so lists ("rooms 101, 102, 103") and times ("09.00 - 17.00") stay prose.
_LOOSE_SEP = r"(?:[ \t]*[-.,/_·•|:;*~'()]{1,3}[ \t]*(?:\n[ \t]*)?|[ \t]*\n[ \t]*|[ \t]+)"
_LOOSE_RUN_RE = re.compile(rf"(?<![0-9])\+?[0-9]+(?:{_LOOSE_SEP}[0-9]+)+")
_LOOSE_SEP_RE = re.compile(_LOOSE_SEP)
_DIGIT_GROUP_RE = re.compile(r"[0-9]+")
# Separators the strict run joins already.
_STRICT_SEPS = frozenset({" ", "-", ".", "/"})
# A quantity ("10 000 000", "1.234.567") or a decimal ("3.14159"), not a phone.
_QUANTITY_RE = re.compile(r"[1-9][0-9]{0,2}(?:(?: [0-9]{3})+|(?:\.[0-9]{3})+)|[0-9]+\.[0-9]+")
_SSN_RE = re.compile(r"[0-9]{3}-[0-9]{2}-[0-9]{4}")
_SSN_GROUPED_RE = re.compile(r"[0-9]{3}[ \t-][0-9]{2}[ \t-][0-9]{4}")
# How cards are printed, for a card followed by more digits ("… 1111 12/26").
_CARD_FORMATS = ((4, 4, 4, 4, 3), (4, 4, 4, 4), (4, 6, 5), (4, 6, 4))

_AMOUNT_NUM = r"[0-9]{1,3}(?:[.,' ][0-9]{3})+(?:[.,][0-9]{1,2})?|[0-9]+(?:[.,][0-9]{1,2})?"
_AMOUNT_RE = re.compile(
    r"(?<![0-9.,])(?:"
    rf"(?P<pre>€|\$|£|\b(?i:eur|usd|gbp|chf|hrk)(?![A-Za-z]))[ \t]?(?P<n1>{_AMOUNT_NUM})"
    # "850,- kn": the Croatian / German price dash after the number
    rf"|(?P<n2>{_AMOUNT_NUM})(?:[.,]-)?[ \t]?(?P<post>€|\$|£|%"
    r"|(?i:eur(?:a|o|os)?|euros?|usd|gbp|chf|hrk|kn|kuna|kune|dollars?|dolara|percent|per[ \t]?cent"
    r"|posto|prozent"
    # the region's other currencies and percent words, Latin and Cyrillic: the
    # writer answers in the sender's language
    r"|evr(?:a|o|ov|ova|ima)?|dinar(?:a|i)?|din|rsd|km|bam|kč|czk|zł(?:otych|oty)?|zl|pln"
    r"|ft|huf|lei|ron|leva?|bgn|odsto|procen(?:at|ata|ta|ti)|procent"
    r"|евр\w*|динар\w*|рубл\w*|одсто|процен\w*)\b)"
    r")(?![0-9])")
_CURRENCY = {
    "€": "eur", "eur": "eur", "eura": "eur", "euro": "eur", "euros": "eur",
    "$": "usd", "usd": "usd", "dollar": "usd", "dollars": "usd", "dolara": "usd",
    "£": "gbp", "gbp": "gbp", "chf": "chf",
    "hrk": "hrk", "kn": "hrk", "kuna": "hrk", "kune": "hrk",
    "%": "%", "percent": "%", "per cent": "%", "posto": "%", "prozent": "%",
    "odsto": "%", "procenat": "%", "procenata": "%", "procenta": "%", "procenti": "%",
    "procent": "%", "одсто": "%",
    "evra": "eur", "evro": "eur", "evrov": "eur", "evrova": "eur", "evrima": "eur",
    "dinar": "rsd", "dinara": "rsd", "dinari": "rsd", "din": "rsd", "rsd": "rsd",
    "km": "bam", "bam": "bam", "kč": "czk", "czk": "czk", "zł": "pln", "zl": "pln",
    "złoty": "pln", "złotych": "pln", "pln": "pln", "ft": "huf", "huf": "huf",
    "lei": "ron", "ron": "ron", "lev": "bgn", "leva": "bgn", "bgn": "bgn",
}
# A bare figure in a note ("850/day", "12k", "850 po danu", "37 points"),
# for the background veto: the inbound mail can't clear a value the writer
# was shown, whatever marks it carried there.
_BARE_FIGURE_RE = re.compile(
    rf"(?<![0-9.,])(?P<n>{_AMOUNT_NUM})(?:[ \t]?(?P<k>[kK]\b|tis\.|tisu[cć]a\b|thousand\b))?"
    r"(?![0-9])")


def _figure_values(text: str) -> set[str]:
    """Every figure in *text* as :func:`_amount_value` writes it (``12k`` as
    ``12000``)."""
    out: set[str] = set()
    for m in _BARE_FIGURE_RE.finditer(text):
        v = _amount_value(m.group("n"))
        out.add(v)
        if m.group("k"):
            try:
                out.add(_amount_value(f"{float(v) * 1000:.2f}"))
            except ValueError:
                pass
    return out

_EMAIL_RE = re.compile(
    r"(?<![\w.%+-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,24}(?![\w-])")
# Addresses written so a filter won't see them: "ivo (at) acme (dot) com",
# "ivo dot horvat at acme dot com", "ivo.horvat@acme", "ivo.horvat@acme .com".
_AT = r"(?:[ \t]*[(\[{][ \t]*at[ \t]*[)\]}][ \t]*|[ \t]+at[ \t]+|[ \t]*@[ \t]*)"
_DOT = r"(?:[ \t]*[(\[{][ \t]*dot[ \t]*[)\]}][ \t]*|[ \t]+dot[ \t]+|[ \t]*\.[ \t]*)"
_OBFUSCATED_EMAIL_RE = re.compile(
    rf"(?i)(?<![\w.%+-])(?P<local>[\w%+-]+(?:{_DOT}[\w%+-]+)*){_AT}"
    rf"(?P<domain>[\w-]+(?:{_DOT}[\w-]+)*)(?![\w-])")
_DOT_SPLIT_RE = re.compile(_DOT, re.IGNORECASE)


def _hidden_emails(text: str) -> list[tuple[int, str]]:
    """(offset, address) for each address written around a filter: with an
    ``(at)``/``at``/``(dot)``/``dot`` spelt out (only when one of them is
    spelt out, or the address has no top-level domain or a space before
    it) — a plain address is :data:`_EMAIL_RE`'s."""
    out: list[tuple[int, str]] = []
    for m in _OBFUSCATED_EMAIL_RE.finditer(text):
        raw = m.group()
        spelt = bool(re.search(r"(?i)\b(?:at|dot)\b", raw))
        if not spelt and _EMAIL_RE.fullmatch(raw.strip()):
            continue                            # an ordinary address
        local = ".".join(_DOT_SPLIT_RE.split(m.group("local")))
        labels = _DOT_SPLIT_RE.split(m.group("domain"))
        if spelt and (len(labels) < 2 or not labels[-1].isalpha()
                      or any(len(label) < 2 for label in labels)):
            continue                            # "meet at venue", "at 6 dot on": no domain
        if spelt and not re.search(r"(?i)[(\[{][ \t]*(?:at|dot)|\bdot\b", raw):
            continue                            # " at " alone needs a "dot" too
        out.append((m.start(), f"{local}@{'.'.join(labels)}".casefold()))
    return out
# Any scheme with "//" ("FTP://", "sftp://"), "https:" without them, "www.".
_URL_RE = re.compile(
    r"(?i)\b(?:[a-z][a-z0-9+.-]{1,15}://|https?:(?=[^\s/])|www\.)[^\s<>\"'`()\[\]{}\x00]+")
_URL_TRAILING = ".,;:!?*"
_SCHEME_RE = re.compile(r"(?i)[a-z][a-z0-9+.-]*://")

# A bare domain ("evil-meetup.com/register"): Gmail, Outlook and Apple Mail
# link it in plain text, so it is a link like any other. The top-level domain
# must be a country code or a common generic one, written in one case
# ("venue.It" is a missing space, not Italy).
_CC_TLDS = frozenset("""
ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br bs bt
bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg er
es et eu fi fj fk fm fo fr ga gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht
hu id ie il im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr
ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz na nc ne nf ng
ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd
se sg sh si sk sl sm sn so sr ss st su sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw
tz ua ug uk us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw
""".split())
_GENERIC_TLDS = frozenset("""
com net org info biz edu gov mil int app dev page xyz online site website store shop tech
cloud link live life world today news blog club pro asia mobi name events email digital agency
studio design media network solutions services center support space fun top icu vip work click
one run gle goog google bar best buzz cam cfd sbs cyou monster rest lol wtf quest win bid loan
men date download review stream racing science party trade webcam zip mov foo new how soy boo
day art music photo photos gallery group global ltd llc inc gmbh company consulting academy
education school university travel tours wine vin pub restaurant cafe tickets community social
chat team zone city land plus tools systems software ninja guru expert codes host hosting domains
press wiki tel jobs careers red blue pink green black gold ink fyi meme help tips works ventures
capital finance money pay bank cash promo sale deals market markets exchange trading berlin
london nyc paris wien amsterdam barcelona eus cat gal scot swiss bayern koeln hamburg tirol coop
aero museum post health care clinic dental fitness yoga golf football games game casino bet poker
""".split())
_LINK_TLDS = _CC_TLDS | _GENERIC_TLDS
# Endings that are file extensions as often as domains ("notes.md",
# "setup.py", "video.mov"): a link only with a path or port after them.
_FILE_TLDS = frozenset("md py sh ps so tf zip mov".split())
# Abbreviations written without spaces ("prof.dr.sc.", "dipl.ing.", "no.one"):
# with one of these as a label, only a path makes it a link.
_ABBREVIATIONS = frozenset("""
dr mr prof dipl mag univ bacc spec ing oec iur med sc st sv ul br tel mob str npr tj itd sl god
vs etc approx no nr vol asp vb ado
""".split())
_BARE_LINK_RE = re.compile(
    r"(?<![\w.@/\\%+~-])"
    r"(?P<host>(?:[^\W_](?:[\w-]{0,61}[^\W_])?\.)+(?P<tld>[^\W\d_]{2,24}))"
    r"(?![\w@-]|\.[^\W_])"
    r"(?P<rest>(?::[0-9]{1,5})?(?:[/?#][^\s<>\"'`()\[\]{}\x00]*)?)")

# Own addresses on these domains don't make the domain the owner's.
_FREE_MAIL_DOMAINS = frozenset("""
gmail.com googlemail.com outlook.com hotmail.com live.com msn.com yahoo.com ymail.com icloud.com
me.com mac.com aol.com proton.me protonmail.com pm.me gmx.com gmx.de gmx.net web.de mail.com
yandex.com yandex.ru zoho.com fastmail.com tutanota.com hey.com t-com.hr net.hr inet.hr vip.hr
""".split())
_FREE_MAIL_NAMES = frozenset(
    "gmail googlemail outlook hotmail live msn yahoo ymail icloud aol gmx yandex proton protonmail"
    .split())

_LOCALHOST_RE = re.compile(r"(?i)(?<![\w.-])localhost(?![\w-])")
_LOCAL_TLD_RE = re.compile(
    r"(?i)(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:local|internal|lan|localdomain)"
    r"(?![\w-]|\.[a-z0-9])")
_IPV4_RE = re.compile(r"(?<![0-9.])([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(?![0-9]|\.[0-9])")
_IPV6_LOOPBACK_RE = re.compile(r"(?<![\w:])::1(?![\w:])")
# Anywhere, links included.
_PATH_ANYWHERE_RE = re.compile(
    r"(?i)(?:file://|\bvfs:|\.captain-claw\b|(?<![A-Za-z0-9])[A-Za-z]:\\|\\\\[A-Za-z0-9._$-]+)"
    r"[^\s\"'<>)\]]*")
# Outside links only: https://example.com/home/ is a web page.
_PATH_OUTSIDE_URL_RE = re.compile(
    r"(?:/Users/|/home/|(?<![\w~/])~/|(?<![\w/.-])saved/(?=[\w.-]+(?:/|\.\w{1,5}\b)))"
    r"[^\s\"'<>)\]]*")

_HTML_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]{0,30}(?:\s[^<>\n]{0,500})?/?>")
_MD_IMAGE_RE = re.compile(r"!\[[^\]\n]{0,500}\]\([^)\n]{0,500}\)")

# Not content words for the shingle floor (EN/HR/DE).
_COMMON_WORDS = frozenset("""
that this with have from your will would could should about there their they them what when
which where while been were into than then also just only very more most some such each other
over after before here thank thanks please hello dear best regards kind
koji koja koje kako kada gdje ovaj ova ovo taj ta to biti bilo bila sam smo ste vam vas nam
hvala molim lijep pozdrav postovani postovana
dass diese dieser dieses haben sind wird werden kann konnen ihnen ihre uber nach auch noch
danke bitte grusse liebe lieber sehr
""".split())


# ── Provenance ───────────────────────────────────────────────────────


def _url_key(url: str) -> tuple[str, str, str, str] | None:
    """(host without ``www.``, path without a trailing slash, query,
    fragment) — two spellings of one link compare equal."""
    # A browser reads "\" in a web link as "/" (WHATWG): the host it opens
    # is the one before it, so the guard must read it the same way.
    u = url.rstrip(_URL_TRAILING).replace("\\", "/")
    if not _SCHEME_RE.match(u):
        u = "http://" + u                   # www.… and bare domains
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None
    if "@" in parts.netloc:
        host = ""                           # userinfo: never cleared, whatever host it names
    if host.startswith("www."):
        host = host[4:]
    return host, parts.path.rstrip("/"), parts.query, parts.fragment


def _bare_link(m: re.Match) -> str:
    """The link a :data:`_BARE_LINK_RE` match is, or ``""`` when it is a
    file name, an abbreviation ("dr.sc.") or a typo rather than a domain."""
    tld = m.group("tld")
    labels = m.group("host").split(".")[:-1]
    if not any(c.isalpha() for label in labels for c in label):
        return ""
    rest = m.group("rest").rstrip(_URL_TRAILING)
    if rest and rest[0] in "/?#:" and len(rest) > 1:
        # A path, query, fragment or port after it: a link on any TLD, in any
        # case ("tickets.conference/register", "meetup-tix.Com/r").
        return m.group("host") + rest
    if not (tld.islower() or tld.isupper()) or tld.lower() not in _LINK_TLDS:
        return ""
    if not rest and (tld.lower() in _FILE_TLDS
                     or (len(tld) == 2 and max(len(label) for label in labels) <= 2)
                     or any(label.lower() in _ABBREVIATIONS for label in labels)):
        return ""
    return m.group("host") + rest


def _links(text: str) -> list[tuple[int, int, str]]:
    """Every link in *text* as (start, end, link): ``http(s)://`` and
    ``www.`` links, then bare domains outside them."""
    out: list[tuple[int, int, str]] = []
    for m in _URL_RE.finditer(text):
        link = m.group().rstrip(_URL_TRAILING)
        out.append((m.start(), m.start() + len(link), link))
    spans = [(s, e) for s, e, _ in out]
    for m in _BARE_LINK_RE.finditer(text):
        link = _bare_link(m)
        end = m.start() + len(link)
        if link and not any(s < end and m.start() < e for s, e in spans):
            out.append((m.start(), end, link))
    out.sort()
    return out


def _path_within(path: str, cleared: str) -> bool:
    """*path* is *cleared* or one of its parent folders (``/ana`` for
    ``/ana/30min``; ``""`` is the site root)."""
    return not path or path == cleared or cleared.startswith(path + "/")


def _owned_domains(addresses: Sequence[str]) -> frozenset[str]:
    """The mail domains of the owner's own addresses, free-mail providers
    left out: links on these hosts (and their subdomains) are the owner's."""
    out: set[str] = set()
    for address in addresses:
        for m in _EMAIL_RE.finditer(_prep(address)):
            domain = m.group().rsplit("@", 1)[1].casefold().rstrip(".")
            if domain in _FREE_MAIL_DOMAINS or domain.split(".", 1)[0] in _FREE_MAIL_NAMES:
                continue
            out.add(domain)
    return frozenset(out)


def _owned_host(host: str, owned: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in owned)


def _amount_value(number: str) -> str:
    """``1.234,50`` and ``1,234.5`` → ``1234.5``; ``40,00`` → ``40``."""
    s = number.replace(" ", "").replace("'", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s or "." in s:
        parts = s.split("," if "," in s else ".")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            s = f"{parts[0]}.{parts[1]}"
        else:
            s = "".join(parts)
    whole, _, frac = s.partition(".")
    whole = whole.lstrip("0") or "0"
    frac = frac.rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def _amount_key(m: re.Match) -> tuple[str, str]:
    """(currency, value) of an :data:`_AMOUNT_RE` match."""
    cur = _squash((m.group("pre") or m.group("post") or "").lower())
    number = m.group("n1") or m.group("n2") or ""
    return _CURRENCY.get(cur, cur), _amount_value(number)


def _same_phone(a: str, b: str) -> bool:
    """Digits of two phone numbers name one line: equal after the trunk
    zeros, or one ends with the other's 8+ digits (``+385 1 234 5678`` and
    ``01 234 5678``) — also with the ``(0)`` trunk an international number
    carries after its country code (``+385 (0)1 234 5678``)."""
    a, b = a.lstrip("0"), b.lstrip("0")

    def same(x: str, y: str) -> bool:
        short, long_ = (x, y) if len(x) <= len(y) else (y, x)
        return len(short) >= 8 and long_.endswith(short)

    if same(a, b):
        return True
    for x, y in ((a, b), (b, a)):
        for i in (1, 2, 3):                 # country codes are 1–3 digits
            if len(x) > i + 8 and x[i] == "0" and same(x[:i] + x[i + 1:], y):
                return True
    return False


def _content_shingles(words: Sequence[str], n: int) -> set[str]:
    out: set[str] = set()
    for i in range(len(words) - n + 1):
        window = words[i:i + n]
        content = sum(1 for w in window if len(w) >= 4 and w not in _COMMON_WORDS)
        if content >= _SHINGLE_CONTENT_WORDS:
            out.add(" ".join(window))
    return out


def _loose_segments(text: str) -> Iterator[tuple[int, int, list[str], bool, bool]]:
    """(start, end, digit groups, starts with ``+``, can't be a phone — it
    crosses a line break, or is a mixed run joined by a list mark)
    for each stretch of a :data:`_LOOSE_RUN_RE` run whose separators are all
    of one kind (spaces and line breaks are one kind), leaving out stretches
    the strict run joins already — and the whole run as well when it mixes
    kinds ("091 / 234 5678", "4111, 1111 - 1111_1111")."""
    for m in _LOOSE_RUN_RE.finditer(text):
        run, base = m.group(), m.start()
        groups = [(g.start(), g.end(), g.group()) for g in _DIGIT_GROUP_RE.finditer(run)]
        seps = [run[groups[i][1]:groups[i + 1][0]] for i in range(len(groups) - 1)]
        sorts = [s.strip(" \t\n") or " " for s in seps]
        if len(set(sorts)) > 1:
            plus = run.startswith("+")
            # A list mark in a mixed run ("091 765 4321, 10 am") ends a
            # phone: such a run counts only as a card or an ID (checksums),
            # never as a phone — flagged like a line break.
            listed = any(c in s for s in seps for c in ",;")
            yield (base + groups[0][0] - plus, base + groups[-1][1],
                   [g for _s, _e, g in groups], plus,
                   listed or any("\n" in s for s in seps))
        i = 0
        while i < len(seps):
            j = i
            while j + 1 < len(seps) and sorts[j + 1] == sorts[i]:
                j += 1
            if not all(s in _STRICT_SEPS for s in seps[i:j + 1]):
                part = groups[i:j + 2]
                plus = i == 0 and run.startswith("+")
                yield (base + part[0][0] - plus, base + part[-1][1],
                       [g for _s, _e, g in part], plus, any("\n" in s for s in seps[i:j + 1]))
            i = j + 1


# The inbound mail vouches for a kind only while it names at most this many
# distinct values of it: a sender who lists "€50 €100 … €3000" or a few
# hundred guessed colleague addresses is fishing, not asking about their own.
_INBOUND_VOUCH_MAX = 5
# …and a digit run of theirs vouches for a number inside it only when it is at
# most this many digits longer ("#2026-00123456" for "00123456"), not a
# thousand-digit run that holds every guess.
_INBOUND_NUMBER_SLACK = 6


class _Corpus:
    """The texts one provenance set holds, normalised every way a detector
    compares: folded, as words, compact, digit runs, emails, amounts, links.

    An *untrusted* corpus (the inbound mail) vouches only for values it names
    few of, see :data:`_INBOUND_VOUCH_MAX`."""

    def __init__(self, texts: Sequence[str], *, untrusted: bool = False):
        prepared = [_prep(t) for t in texts]
        self.untrusted = untrusted
        self.raw = "\n".join(prepared)
        self.folded = _squash(_fold(self.raw))
        self._word_lists = [_words(p) for p in prepared]
        self.words = f" {' '.join(w for ws in self._word_lists for w in ws)} "
        self.compacts = [_compact(p) for p in prepared]
        runs = [_digits(m.group()) for m in _NUM_RUN_RE.finditer(self.raw)]
        runs += ["".join(seg[2]) for seg in _loose_segments(self.raw)]
        self.numbers = [d for d in runs if len(d) >= 4]
        self._long_numbers = len({d for d in self.numbers if len(d) >= 8})
        self.emails = {m.group().casefold() for m in _EMAIL_RE.finditer(self.raw)}
        self.emails |= {addr for _pos, addr in _hidden_emails(self.raw)}
        self.amounts = {_amount_key(m) for m in _AMOUNT_RE.finditer(self.raw)}
        self.figures = _figure_values(self.raw)
        self.urls: set[tuple[str, str, str, str]] = set()
        self.paths: dict[str, set[str]] = {}
        self.sites: set[str] = set()
        for _s, _e, link in _links(self.raw):
            key = _url_key(link)
            if key and key[0]:
                self.urls.add(key)
                self.paths.setdefault(key[0], set()).add(key[1])
                if not (key[1] or key[2] or key[3]):
                    self.sites.add(key[0])      # a whole site: "https://acme.hr"
        self._shingles: dict[int, set[str]] = {}
        self._terms: _TermText | None = None

    @property
    def terms(self) -> _TermText:
        if self._terms is None:
            self._terms = _TermText(self.raw)
        return self._terms

    def shingles(self, n: int) -> set[str]:
        """The *n*-word shingles of each text (none across two texts)."""
        if n not in self._shingles:
            self._shingles[n] = set().union(*(_content_shingles(ws, n) for ws in self._word_lists))
        return self._shingles[n]

    def has_number(self, digits: str, *, phone: bool = False) -> bool:
        if not self.untrusted:
            return any(digits in d or (phone and _same_phone(digits, d)) for d in self.numbers)
        if self._long_numbers > _INBOUND_VOUCH_MAX:
            return False
        return any((digits in d and len(d) - len(digits) <= _INBOUND_NUMBER_SLACK)
                   or (phone and _same_phone(digits, d)) for d in self.numbers)

    def has(self, kind: str, value: Any, digits: str = "") -> bool:
        """Whether this corpus holds the value a *kind* finding is about."""
        if kind in ("card_number", "oib", "jmbg", "ssn", "long_number", "phone_number"):
            return bool(digits) and self.has_number(digits, phone=kind == "phone_number")
        if kind == "iban":
            c = _compact(value)
            return bool(c) and any(c in x for x in self.compacts)
        if kind == "email_address":
            if self.untrusted and len(self.emails) > _INBOUND_VOUCH_MAX:
                return False
            return value.casefold() in self.emails
        if kind == "amount":
            if self.untrusted and len(self.amounts) > _INBOUND_VOUCH_MAX:
                return False
            return value in self.amounts
        if kind == "url":
            return _url_key(value) in self.urls
        if kind == "never_disclose":
            shape = _term_shape(str(value))
            return shape is not None and self.terms.has(shape)
        v = _squash(_fold(str(value)))
        return bool(v) and (v in self.folded or (len(_compact(v)) >= 6
                                                 and any(_compact(v) in x for x in self.compacts)))


_EMPTY = _Corpus(())


# ── The scan ─────────────────────────────────────────────────────────


class _Scan:
    """One pass over one text. ``self.w`` is the working copy: a span a
    detector claims is blanked there, so a later detector doesn't read an
    IBAN's digits as a card or a date as a phone number."""

    def __init__(self, text: str, *, shareable: _Corpus, inbound: _Corpus,
                 holds: bool, ids: bool, background: _Corpus | None = None,
                 owned: frozenset[str] = frozenset()):
        self.t = _prep(text)
        self.w = self.t
        self.shareable = shareable
        self.inbound = inbound
        self.background = background or _EMPTY
        self.owned = owned
        self.holds = holds
        self.ids = ids
        self._found: list[tuple[int, int, Finding]] = []
        self._urls = _links(self.t)

    # bookkeeping

    def _claim(self, start: int, end: int) -> None:
        self.w = self.w[:start] + _BLANK * (end - start) + self.w[end:]

    def _in_url(self, pos: int) -> bool:
        return any(s <= pos < e for s, e, _ in self._urls)

    def add(self, pos: int, kind: str, level: str, value: Any, *, digits: str = "",
            hint_value: str | None = None, echo: bool | None = None) -> None:
        if echo is None:
            echo = self.inbound.has(kind, value, digits)
        hint = _hint(kind, hint_value if hint_value is not None else str(value))
        rank = 0 if level == BLOCK else 1
        self._found.append((rank, pos, Finding(kind, level, hint, bool(echo))))

    def hold(self, pos: int, kind: str, value: Any, *, digits: str = "",
             hint_value: str | None = None) -> None:
        """A hold-class value: nothing when a SHAREABLE source holds it, or the
        inbound mail does and the background doesn't (a sender naming the
        figure an insight holds doesn't clear the insight)."""
        if self.shareable.has(kind, value, digits):
            return
        vetoed = self.background.has(kind, value, digits) or (
            kind == "amount" and value[1] in self.background.figures)
        if self.inbound.has(kind, value, digits) and not vetoed:
            return
        self.add(pos, kind, HOLD, value, digits=digits, hint_value=hint_value)

    def verdict(self) -> Verdict:
        seen: set[Finding] = set()
        out: list[Finding] = []
        for _, _, f in sorted(self._found, key=lambda x: (x[0], x[1])):
            if f not in seen:
                seen.add(f)
                out.append(f)
        return Verdict(tuple(out))

    # block classes

    def secrets(self) -> None:
        for rx in (_PEM_RE, _JWT_RE, *_TOKEN_RES):
            for m in rx.finditer(self.w):
                kind = "jwt" if rx is _JWT_RE else "private_key" if rx is _PEM_RE else "secret_key"
                self.add(m.start(), kind, BLOCK, m.group())
                self._claim(m.start(), m.end())
        for m in _URL_CREDENTIALS_RE.finditer(self.w):
            self.add(m.start(), "url_credentials", BLOCK, m.group())
            self._claim(m.start(), m.end())
        for m in _TOKEN_PARAM_RE.finditer(self.w):
            name, value = m.group(1).lower(), m.group(2)
            if len(value) < (8 if name == "t" else 4):
                continue
            self.add(m.start(2), "token_param", BLOCK, value)
            self._claim(m.start(2), m.end(2))
        self._seed_phrases()
        self._credential_phrases()

    def _seed_phrases(self) -> None:
        run: list[re.Match] = []

        def close() -> None:
            if len(run) >= _SEED_MIN_WORDS:
                value = " ".join(_SEED_NUMBERING_RE.sub("", m.group()) for m in run)
                self.add(run[0].start(), "seed_phrase", BLOCK, value)
            run.clear()

        for m in re.finditer(r"\S+", self.t):
            token = m.group()
            word = _SEED_NUMBERING_RE.sub("", token)
            if not word:
                continue                    # "1." numbering between words
            if _SEED_WORD_RE.fullmatch(word) and word not in _SEED_STOPWORDS:
                run.append(m)
            else:
                close()
        close()

    def _credential_phrases(self) -> None:
        folded = _fold_same_len(self.t)
        matches = [*_CRED_RE.finditer(folded), *_CRED_TERSE_RE.finditer(folded)]
        for m in matches:
            raw = self.t[m.start("val"):m.end("val")].rstrip(_CRED_TRAILING)
            value = raw.casefold()
            if len(raw) < 3 or value in _CRED_SAFE:
                continue
            if raw[0] in "<[({" or set(raw) <= set("*•xX.-_#"):
                continue                    # "<your password>", "********"
            looks = (any(c.isdigit() for c in raw)
                     or any(not c.isalnum() and c not in "-'." for c in raw)
                     or any(c.isupper() for c in raw[1:]))
            if not looks and not m.group("q"):
                # A plain word is a password only after "Password:" with
                # nothing but punctuation after it on the line.
                if not re.match(r"[ \t]*[:=]", m.group("sep")):
                    continue
                rest = self.t[m.end("val"):].split("\n", 1)[0]
                if re.match(r"[^\w]*\w", rest):
                    continue
            self.add(m.start("val"), "credential_phrase", BLOCK, raw)

    def entropy(self) -> None:
        for m in _ENTROPY_RUN_RE.finditer(self.w):
            pieces = [(m.start(), m.group())]
            if self._in_url(m.start()):
                # A link's path segments are judged one by one.
                pieces, pos = [], m.start()
                for seg in m.group().split("/"):
                    pieces.append((pos, seg))
                    pos += len(seg) + 1
            for pos, seg in pieces:
                if len(seg) >= _ENTROPY_MIN_CHARS and _looks_random(seg):
                    self._random_run(pos, seg)
        for m in _HEX_RUN_RE.finditer(self.w):
            run = m.group()
            if any(c.isdigit() for c in run) and any(c.isalpha() for c in run) and len(set(run)) >= 8:
                self._random_run(m.start(), run)

    def _random_run(self, pos: int, run: str) -> None:
        if run not in self.shareable.raw:
            self.add(pos, "high_entropy", BLOCK, run)
        self._claim(pos, pos + len(run))

    def cards(self) -> None:
        for m in _NUM_RUN_RE.finditer(self.w):
            for digits in _card_numbers(m.group()):
                self.add(m.start(), "card_number", BLOCK, digits, digits=digits)
                self._claim(m.start(), m.end())
        # "4111, 1111, 1111, 1111", "4111 - 1111 - …", a card wrapped over two
        # lines: print groups, an issuer prefix and Luhn, or it is a list.
        for start, end, groups, plus, _wrapped in _loose_segments(self.w):
            if plus:
                continue
            sizes = [len(g) for g in groups]
            for i in range(len(groups)):
                for fmt in _CARD_FORMATS:
                    if tuple(sizes[i:i + len(fmt)]) != fmt:
                        continue
                    cand = "".join(groups[i:i + len(fmt)])
                    if _card_like(cand) and _card_issuer(cand):
                        self.add(start, "card_number", BLOCK, cand, digits=cand)
                        self._claim(start, end)

    def internal(self) -> None:
        for rx in (_LOCALHOST_RE, _LOCAL_TLD_RE, _IPV6_LOOPBACK_RE):
            for m in rx.finditer(self.t):
                self.add(m.start(), "internal_host", BLOCK, m.group())
        for m in _IPV4_RE.finditer(self.t):
            octets = [int(g) for g in m.groups()]
            if max(octets) <= 255 and _private_ipv4(*octets):
                self.add(m.start(), "internal_host", BLOCK, m.group())
        for m in _PATH_ANYWHERE_RE.finditer(self.t):
            self.add(m.start(), "local_path", BLOCK, m.group().rstrip(_URL_TRAILING))
        for m in _PATH_OUTSIDE_URL_RE.finditer(self.t):
            if not self._in_url(m.start()):
                self.add(m.start(), "local_path", BLOCK, m.group().rstrip(_URL_TRAILING))

    def canary(self, canary: str) -> None:
        """The writer prompt's per-call marker, whole or just its random tail,
        however it is spaced or cased."""
        marks = {_compact(canary), _compact(canary.rsplit("-", 1)[-1])}
        marks = {m for m in marks if len(m) >= 6}
        compact = _compact(self.t)
        if any(m in compact for m in marks):
            echo = any(m in x for m in marks for x in self.inbound.compacts)
            self.add(len(self.t), "canary", BLOCK, canary, echo=echo)

    def shingles(self, kind: str, sources: Sequence[str], n: int) -> None:
        if not sources:
            return
        theirs = set().union(*(_content_shingles(_words(_prep(src)), n) for src in sources))
        theirs -= self.shareable.shingles(n)        # what the brief says is cleared
        if not theirs:
            return
        hits = _content_shingles(_words(self.t), n) & theirs
        if hits:
            echo = all(h in self.inbound.shingles(n) for h in hits)
            self.add(len(self.t), kind, BLOCK, "", echo=echo)

    def never_disclose(self, terms: Sequence[str]) -> None:
        """The owner's terms, folded, inflected ("Oriona", "Orions"), joined
        ("ProjectOrion") or spaced out ("O r i o n"); an all-digit term as a
        number however it is grouped."""
        if not terms:
            return
        text = _TermText(self.t)
        numbers: list[str] | None = None
        for term in terms:
            shape = _term_shape(term)
            if shape is None:
                continue
            norm, _variants, _compact_form, term_digits, _prefix = shape
            hit = text.has(shape)
            if not hit and term_digits:
                if numbers is None:
                    numbers = [_digits(m.group()) for m in _NUM_RUN_RE.finditer(self.t)]
                    numbers += ["".join(seg[2]) for seg in _loose_segments(self.t)]
                hit = any(term_digits in d or _same_phone(term_digits, d) for d in numbers)
            if hit:
                echo = self.inbound.terms.has(shape) or (
                    bool(term_digits) and self.inbound.has_number(term_digits, phone=True))
                self.add(len(self.t), "never_disclose", BLOCK, norm,
                         hint_value=term, echo=echo)

    # hold classes (and the claims the block classes rely on)

    def ibans(self) -> None:
        """IBANs: a valid one is held; one shaped like an IBAN that fails
        mod-97 is a long number. Either way its digits are claimed."""
        for m in _IBAN_START_RE.finditer(self.w):
            start = m.start()
            if self.w[start] == _BLANK:
                continue
            found = False
            # Single spaces first; then the groups an instructed writer or a
            # hard wrap separates otherwise ("HR12-1001-…", a line break).
            for loose in (False, True):
                groups = _alnum_groups(self.w, start, loose=loose)
                for k in range(len(groups), 0, -1):
                    value = "".join(g for g, _ in groups[:k])
                    if _iban_ok(value):
                        if self.ids:
                            self.hold(start, "iban", value)
                        self._claim(start, groups[k - 1][1])
                        found = True
                        break
                if found:
                    break
            if found or not self.t[start:start + 2].isupper():
                continue
            groups = _alnum_groups(self.w, start)
            while groups and not any(c.isdigit() for c in groups[-1][0]):
                groups.pop()                # trailing words are not the number
            value = "".join(g for g, _ in groups)
            digits = _digits(value)
            if len(value) >= _IBAN_MIN and len(digits) >= 9:
                if self.ids:
                    self.hold(start, "long_number", value, digits=digits, hint_value=value)
                self._claim(start, groups[-1][1])

    def not_numbers(self) -> None:
        for m in _NOT_NUMBER_RE.finditer(self.w):
            digits = _digits(m.group())
            if len(digits) == 11 and _oib_ok(digits):
                continue                        # an OIB in dotted groups, not an address
            self._claim(m.start(), m.end())

    def amounts(self) -> None:
        for m in _AMOUNT_RE.finditer(self.w):
            if self.holds:
                self.hold(m.start(), "amount", _amount_key(m), hint_value=m.group())
            self._claim(m.start(), m.end())

    def numbers(self) -> None:
        for m in _NUM_RUN_RE.finditer(self.w):
            run = m.group()
            digits = _digits(run)
            kind = _number_kind(run, digits)
            if kind is None:
                continue
            if kind == "phone_number":
                if self.holds:
                    self.hold(m.start(), kind, run, digits=digits, hint_value=digits)
            elif self.ids:
                self.hold(m.start(), kind, run, digits=digits, hint_value=digits)
        # The same with the separators only an instructed writer uses
        # ("091, 234, 5678", "+385_91_234_5678", an OIB in groups). A phone
        # has no checksum, so one never spans a line break: two numbers on
        # two lines are two numbers.
        for start, _end, groups, plus, wrapped in _loose_segments(self.w):
            digits = "".join(groups)
            n = len(digits)
            if self.ids and ((n == 11 and _oib_ok(digits)) or (n == 13 and _jmbg_ok(digits))):
                self.hold(start, "oib" if n == 11 else "jmbg", digits, digits=digits,
                          hint_value=digits)
            elif (self.holds and not wrapped and 8 <= n <= 15 and (plus or digits[0] == "0")
                  and all(len(g) <= 5 for g in groups)
                  # (after "+385" an area code may be one digit: "+385 1 / 4567-890")
                  and all(len(g) >= 2 for g in groups[2 if plus else 1:])
                  and max(len(g) for g in groups) >= 3):    # "09.00 - 17.00" is a time range
                self.hold(start, "phone_number", digits, digits=digits, hint_value=digits)

    def emails(self) -> None:
        for m in _EMAIL_RE.finditer(self.w):
            self.hold(m.start(), "email_address", m.group())
        for pos, addr in _hidden_emails(self.w):
            self.hold(pos, "email_address", addr)

    def urls(self) -> None:
        for start, _, url in self._urls:
            key = _url_key(url)
            if key is not None and self._link_cleared(key):
                continue
            # The inbound mail never vouches for a link; it only marks the echo.
            self.add(start, "url", HOLD, url,
                     echo=key is not None and key in self.inbound.urls)

    def _link_cleared(self, key: tuple[str, str, str, str]) -> bool:
        """A link a SHAREABLE source holds; or, with no query or fragment, a
        page on the owner's own mail domain (not its subdomains, and not a
        link only BACKGROUND holds), a site a SHAREABLE source links whole,
        or a parent folder of a SHAREABLE link. Any
        other path on a SHAREABLE link's host is not cleared: on forms.gle,
        calendly.com or docs.google.com anyone can make a page."""
        if key in self.shareable.urls:
            return True
        host, path, query, fragment = key
        if not host or query or fragment:
            return False
        if host in self.shareable.sites:
            return True
        if host in self.owned and key not in self.background.urls:
            # A page on the owner's own mail domain itself — not a subdomain
            # ("cloud.acme.hr/s/…", "jira.acme.hr/…" need a SHAREABLE source
            # like any link), and not a link the writer only saw in BACKGROUND.
            return True
        return any(_path_within(path, p) for p in self.shareable.paths.get(host, ()))

    def shape(self) -> None:
        for m in _HTML_RE.finditer(self.t):
            self.add(m.start(), "html", HOLD, m.group())
        for m in _MD_IMAGE_RE.finditer(self.t):
            self.add(m.start(), "markdown_image", HOLD, m.group())


def _alnum_groups(text: str, start: int, *, loose: bool = False) -> list[tuple[str, int]]:
    """The alphanumeric groups from *start* that single spaces join
    (``HR12 1001 0051 …``), uppercased, each with its end offset; at most
    :data:`_IBAN_MAX` characters in all. *loose* joins across any
    :data:`_LOOSE_SEP` (``HR12-1001``, ``HR12, 1001``, a line break) but
    only groups with a digit in them, past a bank code of 4 letters at most
    (``GB82 WEST 1234 …``), so the words after an IBAN stay words."""
    groups: list[tuple[str, int]] = []
    total, i = 0, start
    while i < len(text) and total < _IBAN_MAX:
        j = i
        while j < len(text) and text[j].isascii() and text[j].isalnum():
            j += 1
        if j == i:
            break
        group = text[i:j].upper()
        if (loose and groups and not any(c.isdigit() for c in group)
                and not (len(groups) == 1 and len(group) <= 4)):
            break
        groups.append((group, j))
        total += j - i
        if loose:
            sep = _LOOSE_SEP_RE.match(text, j)
            nxt = sep.end() if sep else j
        else:
            nxt = j + 1 if j < len(text) and text[j] == " " else j
        if j < nxt < len(text) and text[nxt].isascii() and text[nxt].isalnum():
            i = nxt
        else:
            break
    return groups


def _looks_random(run: str) -> bool:
    """A key, not a word or a slug: three character classes and Shannon
    entropy of 4 bits or more, and not lowercase words and numbers joined by
    ``-``, ``_`` or ``/``."""
    classes = (any(c.islower() for c in run) + any(c.isupper() for c in run)
               + any(c.isdigit() for c in run) + any(c in "+/=_-" for c in run))
    if classes < 3:
        return False
    if not any(c.isupper() for c in run) and all(
            p.isalpha() or p.isdigit() for p in _SLUG_SPLIT_RE.split(run) if p):
        return False
    counts = Counter(run)
    bits = -sum(n / len(run) * math.log2(n / len(run)) for n in counts.values())
    return bits >= _ENTROPY_MIN_BITS


def _card_like(digits: str) -> bool:
    # No payment card starts with 0; a valid 13-digit JMBG is read as one.
    return (13 <= len(digits) <= 19 and digits[0] != "0"
            and not (len(digits) == 13 and _jmbg_ok(digits)) and _luhn_ok(digits))


def _card_issuer(digits: str) -> bool:
    """A card network's prefix: 3 (Amex, Diners, JCB), 4 (Visa), 5 and
    2221–2720 (Mastercard), 6 (Discover, Maestro, UnionPay). Four years in a
    row ("2019, 2020, 2021, 2022") are not a card."""
    return digits[0] in "3456" or 2221 <= int(digits[:4]) <= 2720


def _card_numbers(run: str) -> list[str]:
    """The card numbers in a digit run: the whole stretch (13–19 digits,
    spaces and dashes allowed), else card-formatted groups inside a longer
    one. A ``+`` or ``(`` start is a phone number."""
    if run[0] in "+(":
        return []
    out: list[str] = []
    for part in re.split(r"[./()]", run):
        groups = [g for g in re.split(r"[ \t-]", part) if g]
        if not groups:
            continue
        whole = "".join(groups)
        if _card_like(whole):
            out.append(whole)
            continue
        if len(groups) < 2 or len(whole) < 13:
            continue
        sizes = [len(g) for g in groups]
        for i in range(len(groups)):
            for fmt in _CARD_FORMATS:
                if tuple(sizes[i:i + len(fmt)]) == fmt:
                    cand = "".join(groups[i:i + len(fmt)])
                    if _card_like(cand) and cand not in out:
                        out.append(cand)
    return out


def _number_kind(run: str, digits: str) -> str | None:
    """What a digit run of 8+ digits is: an SSN shape, a checksummed OIB or
    JMBG, a phone number (8–15 digits, written like one), else a long number
    (9+). None for anything shorter."""
    n = len(digits)
    if n < 8:
        return None
    if _QUANTITY_RE.fullmatch(run):
        return "long_number" if n >= 9 else None
    if _SSN_RE.fullmatch(run):
        return "ssn"
    contiguous = run.isdigit()
    if _SSN_GROUPED_RE.fullmatch(run):
        return "ssn"
    # An OIB or JMBG whatever its grouping ("694 351 515 30"), unless it is
    # written like a phone number (+ or a bracket in front).
    if run[0] not in "+(" and n == 11 and _oib_ok(digits):
        return "oib"
    if run[0] not in "+(" and n == 13 and _jmbg_ok(digits):
        return "jmbg"
    shaped = run[0] in "+(" or digits[0] == "0" or not contiguous
    if shaped and n <= 15:
        return "phone_number"
    return "long_number" if n >= 9 else "phone_number"


def _private_ipv4(a: int, b: int, c: int, d: int) -> bool:
    return (a in (10, 127) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
            or (a == 169 and b == 254) or (a, b, c, d) == (0, 0, 0, 0))


# ── Entry points ─────────────────────────────────────────────────────


def _checked_text(text: Any) -> str:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if len(text) > MAX_SCAN_CHARS:
        raise ValueError("text too long to scan")
    return text


def scan_reply(reply: str, *, shareable: Sequence[str], inbound: Sequence[str],
               own_addresses: Sequence[str], never_disclose: Sequence[str], canary: str,
               prompt_texts: Sequence[str], private_corpus: Sequence[str],
               background: Sequence[str] = ()) -> Verdict:
    """The full layer-1 scan of a writer's reply body, before the signature
    is added.

    *shareable* is what the reply may state (brief, the folder excerpts the
    writer was shown, the signature); *own_addresses* join it, and their
    domains (free-mail providers aside) clear any link on the owner's hosts.
    *inbound* is what the sender already wrote (subject, body, their address,
    the thread text included); it vouches for a kind only while it names few
    values of it. *never_disclose* are the owner's terms, *canary* the
    per-call marker in the writer prompt, *prompt_texts* the static Flight
    Deck prompts (writer, lookup, classifier, judge; not the composed
    messages, which hold the email), *private_corpus* the owner profile's
    ``about_me`` and ``instructions``. *background* is what the writer was
    shown but not cleared (insights, playbooks, web results): never
    provenance, and a value it holds needs a SHAREABLE source even when the
    sender wrote it too. Shingles a SHAREABLE source also holds are
    cleared."""
    try:
        text = _checked_text(reply)
        own = _texts(own_addresses)
        scan = _Scan(text, shareable=_Corpus(_texts(shareable) + own),
                     inbound=_Corpus(_texts(inbound), untrusted=True), holds=True, ids=True,
                     background=_Corpus(_texts(background)), owned=_owned_domains(own))
        _run(scan, never_disclose=_texts(never_disclose), canary=str(canary or ""),
             prompt_texts=_texts(prompt_texts), private_corpus=_texts(private_corpus))
        return scan.verdict()
    except Exception:
        return _ERROR_VERDICT


def scan_material(text: str, *, never_disclose: Sequence[str]) -> Verdict:
    """The knowledge pre-scan for one folder chunk or lookup result: the
    block classes plus IDs, with no provenance. The caller drops the chunk on
    any finding."""
    try:
        scan = _Scan(_checked_text(text), shareable=_EMPTY, inbound=_EMPTY,
                     holds=False, ids=True)
        _run(scan, never_disclose=_texts(never_disclose))
        return scan.verdict()
    except Exception:
        return _ERROR_VERDICT


def scan_blocks(text: str, *, never_disclose: Sequence[str], shareable: Sequence[str] = (),
                own_addresses: Sequence[str] = (), canary: str = "",
                prompt_texts: Sequence[str] = (), private_corpus: Sequence[str] = ()) -> Verdict:
    """Only the block classes, for a held reply the owner sends or edits:
    the hold classes are theirs to judge. *shareable* keeps a brief's own
    form link from reading as a secret-looking code; the other arguments are
    as in :func:`scan_reply`, each optional."""
    try:
        scan = _Scan(_checked_text(text),
                     shareable=_Corpus(_texts(shareable) + _texts(own_addresses)),
                     inbound=_EMPTY, holds=False, ids=False)
        _run(scan, never_disclose=_texts(never_disclose), canary=str(canary or ""),
             prompt_texts=_texts(prompt_texts), private_corpus=_texts(private_corpus))
        return scan.verdict()
    except Exception:
        return _ERROR_VERDICT


def _run(scan: _Scan, *, never_disclose: Sequence[str], canary: str = "",
         prompt_texts: Sequence[str] = (), private_corpus: Sequence[str] = ()) -> None:
    """Every detector the scan's flags allow, in claiming order: secrets and
    random runs, then IBANs, dates and IP addresses (whose digits are no
    card's or phone's), cards, amounts, the remaining digit runs, addresses
    and links."""
    scan.secrets()
    scan.entropy()
    scan.ibans()
    scan.not_numbers()
    scan.cards()
    scan.amounts()
    if scan.holds or scan.ids:
        scan.numbers()
    if scan.holds:
        scan.emails()
    scan.internal()
    if scan.holds:
        scan.urls()
        scan.shape()
    scan.canary(canary)
    scan.shingles("prompt_echo", prompt_texts, _PROMPT_SHINGLE)
    scan.shingles("private_copy", private_corpus, _PRIVATE_SHINGLE)
    scan.never_disclose(never_disclose)
