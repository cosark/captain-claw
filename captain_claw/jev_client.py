"""Jev (TypeSafe AI) — calibrated decisions about text, for every surface.

Jev is TypeSafe's "System One" model. It does not write text: it takes a
``state`` (text, a JSON object or an array) and named, typed questions, and
answers each one with probabilities (https://docs.typesafe.ai/api):

* ``noul``   — a yes/no question; the probability the answer is yes.
* ``choice`` — one option out of 2..255 you name; per-option probabilities
  and a confidence.
* ``score``  — a level on an ordered rubric of 2..10 levels (lowest first);
  the expected level, per-level probabilities and a confidence.

``POST {base}/v1/systemone`` asks, ``GET {base}/v1/models`` lists the models;
both take ``Authorization: Bearer <key>``.

One module for every caller:

* Flight Deck (``flight_deck/jev_routes.py``) keeps each user's key and calls
  :class:`JevAPI` for the Connections playground, flow steps, the inbound MCP
  server and agents. An agent under Flight Deck never holds the key — it asks
  Flight Deck (:func:`ask_via_flight_deck`), which uses its owner's key.
* The agent tool (``tools/jev.py``) outside Flight Deck calls Jev itself with
  ``tools.jev.api_key`` / ``JEV_API_KEY`` (:func:`standalone_api_key`).

The pure helpers (:func:`normalize_questions`, :func:`parse_state`,
:func:`format_answers`) validate and shape requests the same way everywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx

from captain_claw.logging import get_logger

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
QUESTION_TYPES = ("noul", "choice", "score")

# API limits (docs.typesafe.ai/api, /models): Choice 2..255 options, Score up
# to 10 levels (at least two to mean anything), 32k tokens for the state plus
# the longest question. MAX_QUESTIONS is ours — a runaway guard, not an API
# limit (the docs pack hundreds of questions into one call).
MIN_OPTIONS, MAX_OPTIONS = 2, 255
MIN_LEVELS, MAX_LEVELS = 2, 10
MAX_QUESTIONS = 256
MAX_STATE_CHARS = 120_000  # ≈ 30k tokens

NOT_CONNECTED = (
    "Jev isn't connected — add your Jev API key in Flight Deck → Connections → Jev."
)
NO_KEY_STANDALONE = (
    "Jev needs an API key: set tools.jev.api_key in config.yaml or the "
    "JEV_API_KEY environment variable (keys: console.typesafe.ai/keys)."
)
MEMBER_REFUSED = (
    "Jev isn't available during a shared-agent member's turn — only the agent's "
    "owner can use their Jev key through it."
)

# Accepted spellings for the question types (other gateways say "boolean").
_TYPE_ALIASES = {
    "noul": "noul", "yes_no": "noul", "yesno": "noul", "yes/no": "noul",
    "boolean": "noul", "bool": "noul",
    "choice": "choice", "select": "choice", "classify": "choice",
    "score": "score", "rating": "score", "rate": "score",
}

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
_MAX_RETRY_WAIT = 8.0


class JevInputError(ValueError):
    """The request is malformed (our own validation) — fix the input."""


class JevError(RuntimeError):
    """Jev (or Flight Deck in front of it) refused or failed.

    ``status`` is the HTTP status (0 when Jev couldn't be reached);
    ``retry_after`` the seconds Jev asked to wait (429 / 529), when it said.
    The message is meant for the user / model as-is.
    """

    def __init__(self, status: int, message: str, *, retry_after: float | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(message)


# ── request shaping ─────────────────────────────────────────────────


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return True


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _choice_criteria(q: dict[str, Any], name: str) -> dict[str, Any]:
    raw = q.get("criteria") if q.get("criteria") is not None else q.get("options")
    if raw is None:
        raise JevInputError(f"question '{name}': a choice needs options (criteria)")
    criteria: dict[str, Any] = {}
    if isinstance(raw, dict):
        for option, desc in raw.items():
            criteria[str(option).strip()] = _clean(desc) if _nonempty(desc) else None
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                option = str(item.get("name") or item.get("option") or item.get("value") or "").strip()
                desc = item.get("description", item.get("desc"))
                criteria_desc = _clean(desc) if _nonempty(desc) else None
            else:
                option, criteria_desc = str(item if item is not None else "").strip(), None
            if not option:
                raise JevInputError(f"question '{name}': every option needs a name")
            if option in criteria:
                raise JevInputError(f"question '{name}': option '{option}' appears twice")
            criteria[option] = criteria_desc
    else:
        raise JevInputError(
            f"question '{name}': choice options must be a list of names or a map of name → description"
        )
    if "" in criteria:
        raise JevInputError(f"question '{name}': every option needs a name")
    if not MIN_OPTIONS <= len(criteria) <= MAX_OPTIONS:
        raise JevInputError(
            f"question '{name}': a choice needs {MIN_OPTIONS}–{MAX_OPTIONS} options, got {len(criteria)}"
        )
    return criteria


def _score_criteria(q: dict[str, Any], name: str) -> list[Any]:
    raw = q.get("criteria") if q.get("criteria") is not None else q.get("levels")
    if not isinstance(raw, list):
        raise JevInputError(
            f"question '{name}': a score needs levels (criteria) — an ordered list, lowest first"
        )
    levels = [_clean(level) for level in raw]
    if any(not _nonempty(level) for level in levels):
        raise JevInputError(f"question '{name}': every score level needs a description")
    if not MIN_LEVELS <= len(levels) <= MAX_LEVELS:
        raise JevInputError(
            f"question '{name}': a score needs {MIN_LEVELS}–{MAX_LEVELS} levels, got {len(levels)}"
        )
    return levels


def _noul_criteria(q: dict[str, Any], name: str) -> dict[str, Any] | None:
    raw = q.get("criteria")
    if raw is None:
        yes = q.get("yes_means", q.get("true_means"))
        no = q.get("no_means", q.get("false_means"))
        raw = {k: v for k, v in (("true", yes), ("false", no)) if _nonempty(v)}
        return raw or None
    if not isinstance(raw, dict):
        raise JevInputError(
            f"question '{name}': yes/no criteria must be an object with 'true' and/or 'false'"
        )
    criteria: dict[str, Any] = {}
    for key, value in raw.items():
        k = str(key).strip().lower()
        k = {"yes": "true", "no": "false"}.get(k, k)
        if k not in ("true", "false"):
            raise JevInputError(
                f"question '{name}': yes/no criteria take only 'true' and 'false', not '{key}'"
            )
        if _nonempty(value):
            criteria[k] = _clean(value)
    return criteria or None


def _load_json_text(raw: Any, what: str) -> Any:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise JevInputError(f"{what} is empty")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise JevInputError(f"{what} is not valid JSON: {exc.msg}") from None
    return raw


def normalize_questions(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate *raw* questions and return the API's ``questions`` map.

    Accepts the API form (a map of name → question with ``criteria``), a list
    of questions each carrying a ``name``, or either one as a JSON string. A
    question's options / levels / meaning of yes and no may also be given as
    ``options`` (choice: names, or ``{name, description}`` objects),
    ``levels`` (score) and ``yes_means`` / ``no_means`` (noul).
    """
    raw = _load_json_text(raw, "questions")
    # Forgiving about two shapes models and hand-written flows produce: a
    # {"questions": ...} wrapper, and one bare question instead of a list. (A
    # real name → question map holds objects, never a string "type".)
    if isinstance(raw, dict) and set(raw) == {"questions"}:
        raw = _load_json_text(raw["questions"], "questions")
    if isinstance(raw, dict) and isinstance(raw.get("type"), str) and not isinstance(
        raw.get("instructions", raw.get("question")), dict
    ):
        raw = [raw]
    items: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        items = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, list):
        for i, q in enumerate(raw, 1):
            if not isinstance(q, dict):
                raise JevInputError(f"question #{i} must be an object")
            items.append((str(q.get("name") or q.get("id") or f"q{i}"), q))
    else:
        raise JevInputError("questions must be a list of questions or a map of name → question")
    if not items:
        raise JevInputError("at least one question is required")
    if len(items) > MAX_QUESTIONS:
        raise JevInputError(f"at most {MAX_QUESTIONS} questions per call, got {len(items)}")

    out: dict[str, dict[str, Any]] = {}
    for name, q in items:
        name = name.strip()
        if not name:
            raise JevInputError("every question needs a name")
        if name in out:
            raise JevInputError(f"question name '{name}' is used twice")
        if not isinstance(q, dict):
            raise JevInputError(
                f"question '{name}' must be an object with type and instructions — give "
                "questions as a list of question objects (each with a name) or a map of "
                "name → question object"
            )
        qtype = _TYPE_ALIASES.get(str(q.get("type") or "").strip().lower())
        if not qtype:
            raise JevInputError(
                f"question '{name}': type must be one of noul (yes/no), choice, score"
            )
        instructions = q.get("instructions", q.get("question"))
        if not _nonempty(instructions):
            raise JevInputError(f"question '{name}': instructions (the question itself) are required")
        question: dict[str, Any] = {"type": qtype, "instructions": _clean(instructions)}
        if qtype == "choice":
            question["criteria"] = _choice_criteria(q, name)
        elif qtype == "score":
            question["criteria"] = _score_criteria(q, name)
        else:
            criteria = _noul_criteria(q, name)
            if criteria:
                question["criteria"] = criteria
        out[name] = question
    return out


def parse_state(raw: Any) -> str | dict[str, Any] | list[Any]:
    """The ``state`` to send: an object / array as-is; a string that holds a
    JSON object or array is parsed (so structured state survives tool and
    form inputs, which are text); any other string is sent as text."""
    if isinstance(raw, (dict, list)):
        state: Any = raw
    elif raw is None:
        raise JevInputError("state is required — the content the questions are about")
    else:
        text = str(raw).strip()
        if not text:
            raise JevInputError("state is empty — give the content the questions are about")
        state = text
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                state = parsed
    if isinstance(state, (dict, list)) and not state:
        raise JevInputError("state is empty — give the content the questions are about")
    size = len(state) if isinstance(state, str) else len(json.dumps(state, ensure_ascii=False))
    if size > MAX_STATE_CHARS:
        raise JevInputError(
            f"state is {size:,} characters; Jev reads about {MAX_STATE_CHARS:,} "
            "(~30k tokens). Send only the part the questions need."
        )
    return state


def build_request(state: Any, questions: Any, model: str | None = None) -> dict[str, Any]:
    """A validated ``POST /v1/systemone`` body."""
    return {
        "model": (str(model or "").strip() or DEFAULT_MODEL),
        "state": parse_state(state),
        "questions": normalize_questions(questions),
    }


# ── reading answers ─────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    n = _num(value)
    return "?" if n is None else f"{n:.2f}"


def _top(probs: Any, limit: int = 5) -> str:
    if not isinstance(probs, dict) or not probs:
        return ""
    ranked = sorted(probs.items(), key=lambda kv: -(_num(kv[1]) or 0.0))
    shown = ", ".join(f"{k} {_fmt(v)}" for k, v in ranked[:limit])
    rest = len(ranked) - limit
    return shown + (f", +{rest} more" if rest > 0 else "")


def answer_value(answer: Any) -> Any:
    """The answer's headline value: p(yes) for a noul, the chosen option for a
    choice, the expected level for a score."""
    if not isinstance(answer, dict):
        return None
    kind = answer.get("type")
    if kind == "noul":
        return _num(answer.get("noul"))
    if kind == "choice":
        return answer.get("choice")
    if kind == "score":
        return _num(answer.get("score"))
    return None


def answer_values(response: dict[str, Any]) -> dict[str, Any]:
    """``{question name: headline value}`` — handy for branching on answers."""
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        return {}
    return {name: answer_value(a) for name, a in answers.items()}


def _noul_word(p: float | None) -> str:
    if p is None:
        return "?"
    if p >= 0.65:
        return "yes"
    if p <= 0.35:
        return "no"
    return "uncertain"


def format_answers(response: dict[str, Any]) -> str:
    """A compact, readable account of a ``/v1/systemone`` response."""
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict) or not answers:
        return "Jev returned no answers."
    lines = []
    for name, a in answers.items():
        if not isinstance(a, dict):
            lines.append(f"- {name}: {a!r}")
            continue
        kind = a.get("type")
        if kind == "noul":
            p = _num(a.get("noul"))
            lines.append(f"- {name} [yes/no]: p(yes) = {_fmt(p)} → {_noul_word(p)}")
        elif kind == "choice":
            line = f"- {name} [choice]: {a.get('choice')} (confidence {_fmt(a.get('confidence'))})"
            top = _top(a.get("probabilities"))
            lines.append(line + (f" · {top}" if top else ""))
        elif kind == "score":
            score = _num(a.get("score"))
            legend = a.get("legend") if isinstance(a.get("legend"), dict) else {}
            span = ""
            if legend:
                keys = sorted(int(k) for k in legend if str(k).lstrip("-").isdigit())
                if keys:
                    span = f" on {keys[0]}–{keys[-1]}"
            line = f"- {name} [score]: {_fmt(score)}{span} (confidence {_fmt(a.get('confidence'))})"
            if score is not None and legend:
                nearest = legend.get(str(int(round(score))))
                if nearest is not None:
                    text = nearest if isinstance(nearest, str) else json.dumps(nearest, ensure_ascii=False)
                    line += f' · nearest level {int(round(score))}: "{text[:80]}"'
            lines.append(line)
        else:
            lines.append(f"- {name}: {json.dumps(a, ensure_ascii=False)[:300]}")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    footer = f"model {response.get('model') or '?'}"
    if usage.get("input_tokens") is not None:
        footer += f" · {usage.get('input_tokens')} input tokens"
    return "\n".join(lines) + f"\n({footer})"


# ── HTTP client ─────────────────────────────────────────────────────


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "").strip()[:300]
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, list):  # FastAPI-style validation errors
        parts = []
        for item in detail[:5]:
            if isinstance(item, dict):
                loc = ".".join(str(p) for p in (item.get("loc") or []) if p != "body")
                parts.append(f"{loc}: {item.get('msg')}" if loc else str(item.get("msg")))
            else:
                parts.append(str(item))
        return "; ".join(parts)[:500]
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or json.dumps(detail))[:500]
    if detail is None and isinstance(body, dict):
        return str(body.get("message") or body.get("error") or "")[:500]
    return str(detail or "")[:500]


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _jev_error(resp: httpx.Response) -> JevError:
    status = resp.status_code
    detail = _error_detail(resp)
    request_id = resp.headers.get("x-typesafe-request-id", "")
    suffix = f" (request {request_id})" if request_id else ""
    if status == 401:
        msg = "Jev refused the API key (401) — check that the key is correct and active"
    elif status == 403:
        msg = f"Jev denied access (403){': ' + detail if detail else ''}"
    elif status == 404:
        msg = f"Jev: not found (404){': ' + detail if detail else ''} — is the model name right?"
    elif status == 422:
        msg = f"Jev rejected the request (422){': ' + detail if detail else ''}"
    elif status == 429:
        msg = "Jev rate limit reached (429) — try again shortly"
    elif status == 529:
        msg = "Jev is overloaded (529) — try again shortly"
    else:
        msg = f"Jev API error (HTTP {status}){': ' + detail if detail else ''}"
    return JevError(status, msg + suffix, retry_after=_retry_after(resp))


class JevAPI:
    """Talks to the Jev API with one key. Retries 429 / 529 / 5xx and
    transport errors with backoff (honouring ``Retry-After``, capped)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = (str(base_url or "").strip() or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._client = client

    async def _send(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if not self.api_key:
            raise JevError(0, "No Jev API key")
        if not self.api_key.isascii() or any(c.isspace() for c in self.api_key):
            # It can't go in an HTTP header — and can't be a real key.
            raise JevError(
                401,
                "The Jev API key has characters a key can't have (hidden spaces or "
                "curly quotes from copying) — copy it again",
            )
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                if self._client is not None:
                    resp = await self._client.request(method, url, json=body, headers=headers, timeout=self.timeout)
                else:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        resp = await client.request(method, url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(min(_MAX_RETRY_WAIT, 0.5 * (3 ** (attempt - 1))))
                    continue
                kind = "timed out" if isinstance(exc, httpx.TimeoutException) else "failed"
                raise JevError(0, f"Could not reach Jev ({kind}: {type(exc).__name__})") from None
            if resp.status_code < 400:
                try:
                    data = resp.json()
                except ValueError:
                    raise JevError(resp.status_code, "Jev returned a non-JSON response") from None
                if not isinstance(data, dict):
                    raise JevError(resp.status_code, "Jev returned an unexpected response")
                return data
            if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                wait = _retry_after(resp)
                if wait is None:
                    wait = 0.5 * (3 ** (attempt - 1))
                await asyncio.sleep(min(_MAX_RETRY_WAIT, wait))
                continue
            raise _jev_error(resp)

    async def ask(self, state: Any, questions: Any, model: str | None = None) -> dict[str, Any]:
        """``POST /v1/systemone``; validates first (:func:`build_request`)."""
        body = build_request(state, questions, model)
        data = await self._send("POST", "/v1/systemone", body)
        if not isinstance(data.get("answers"), dict):
            raise JevError(200, "Jev returned no answers")
        return data

    async def models(self) -> list[dict[str, Any]]:
        """``GET /v1/models`` — also a cheap check that the key works."""
        data = await self._send("GET", "/v1/models", None)
        models = data.get("models")
        return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []


def format_models(models: list[dict[str, Any]]) -> str:
    if not models:
        return "Jev lists no models for this key."
    lines = [
        f"- {m.get('name')}" + (f" ({m.get('release_date')})" if m.get("release_date") else "")
        + (f" — {m.get('description')}" if m.get("description") else "")
        for m in models
    ]
    return "Jev models:\n" + "\n".join(lines)


def key_hint(api_key: str) -> str:
    """``····ab12`` — enough to recognise a key, never enough to use it."""
    key = str(api_key or "").strip()
    return f"····{key[-4:]}" if len(key) >= 8 else ("····" if key else "")


# ── standalone (no Flight Deck) ─────────────────────────────────────


def standalone_api_key() -> str:
    """The agent's own key: ``tools.jev.api_key``, else ``JEV_API_KEY`` /
    ``TYPESAFE_API_KEY`` (the official SDKs' name)."""
    key = ""
    try:
        from captain_claw.config import get_config

        key = str(getattr(get_config().tools.jev, "api_key", "") or "").strip()
    except Exception:
        key = ""
    if not key:
        key = (os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY") or "").strip()
    return key


def standalone_api() -> JevAPI:
    from captain_claw.config import get_config

    cfg = get_config().tools.jev
    # Three attempts plus backoff must fit inside the tool's own 150 s.
    timeout = min(float(getattr(cfg, "timeout", 30.0) or 30.0), 40.0)
    return JevAPI(
        standalone_api_key(),
        base_url=str(getattr(cfg, "base_url", "") or DEFAULT_BASE_URL),
        timeout=timeout,
    )


# ── under Flight Deck: the owner's key stays in Flight Deck ─────────

# Whether the owner has a Jev key in Flight Deck, readable synchronously by the
# tool registry (which can't await). Refreshed once per agent turn by
# :func:`refresh_connected` and by every call; stale entries read as "not
# connected" so the tool hides until freshly checked.
_CONNECTED: tuple[bool, float] = (False, 0.0)
_CACHE_MAX_AGE = 120.0
# Flight Deck is asked at most every _REFRESH_MIN_GAP while connected (in
# between, the flag is re-stamped so a long turn can't age it out from under a
# call). "Not connected" is re-checked every time — a key the user just saved
# must show up on the next turn, and the check is a local lookup in Flight Deck.
_REFRESH_MIN_GAP = 20.0
_LAST_CHECK_AT = 0.0


def _mark(connected: bool) -> None:
    global _CONNECTED
    _CONNECTED = (bool(connected), time.time())


def is_jev_connected_cached(max_age: float | None = None) -> bool:
    connected, at = _CONNECTED
    age = time.time() - at
    return connected and at > 0 and age <= (_CACHE_MAX_AGE if max_age is None else max_age)


def _fd_base() -> str:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._flight_deck_base()


def under_flight_deck() -> bool:
    return bool(_fd_base())


def _member_call() -> bool:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._member_call()


def _fd_headers() -> dict[str, str]:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    # Owner-only: never the member grant — /fd/jev/* is not grant-aware.
    return GoogleOAuthManager._flight_deck_headers(as_speaker=False)


def jev_usable() -> bool:
    """Whether the jev tool should be offered right now (sync, for the tool
    registry): under Flight Deck, the owner's freshly checked connection;
    standalone, a configured key."""
    if under_flight_deck():
        return is_jev_connected_cached()
    return bool(standalone_api_key())


async def refresh_connected(force: bool = False) -> bool:
    """Re-check whether the owner has a Jev key in Flight Deck (while
    connected, at most every ~20 s). A member's turn reports not connected and
    leaves the owner's flag alone; an unreachable Flight Deck keeps the previous
    flag."""
    global _LAST_CHECK_AT
    if not under_flight_deck():
        return bool(standalone_api_key())
    if _member_call():
        return False
    connected, _at = _CONNECTED
    if not force and connected and time.time() - _LAST_CHECK_AT < _REFRESH_MIN_GAP:
        _mark(True)
        return True
    _LAST_CHECK_AT = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_fd_base()}/fd/jev/agent/status", headers=_fd_headers())
    except Exception as exc:
        log.debug("Flight Deck Jev status check failed: %s", exc)
        return is_jev_connected_cached()
    if resp.status_code != 200:
        # 404: a Flight Deck without Jev routes; 401/403: this agent can't use
        # its owner's connections. Either way the tool can't work.
        _mark(False)
        return False
    try:
        connected = bool((resp.json() or {}).get("connected"))
    except Exception:
        connected = False
    _mark(connected)
    return connected


# Flight Deck's own Jev client (JevAPI defaults) may take 3 × 30 s plus capped
# backoff before it answers; wait a little longer than that.
_FD_TIMEOUT = 3 * 30.0 + 2 * _MAX_RETRY_WAIT + 10.0


async def _fd_call(method: str, path: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    if _member_call():
        raise JevError(0, MEMBER_REFUSED)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{_fd_base()}{path}", json=body, headers=_fd_headers())
    except httpx.TimeoutException:
        raise JevError(
            0, "Flight Deck didn't answer in time — Jev may be slow or down; try again shortly",
        ) from None
    except Exception as exc:
        raise JevError(0, f"Could not reach Flight Deck for Jev: {type(exc).__name__}") from None
    if resp.status_code == 200:
        _mark(True)
        try:
            data = resp.json()
        except ValueError:
            raise JevError(200, "Flight Deck returned a non-JSON Jev response") from None
        return data if isinstance(data, dict) else {}
    detail = _error_detail(resp)
    if resp.status_code == 404 and detail in ("", "Not Found"):
        # FastAPI's own 404: the route doesn't exist on this Flight Deck.
        detail = "this Flight Deck has no Jev support yet — it needs an update and a restart"
    if resp.status_code == 404 or resp.status_code in (401, 403):
        _mark(False)
    raise JevError(
        resp.status_code,
        detail or f"Flight Deck refused the Jev request (HTTP {resp.status_code})",
        retry_after=_retry_after(resp),
    )


async def ask_via_flight_deck(
    state: Any, questions: Any, model: str | None = None, *, timeout: float = _FD_TIMEOUT,
) -> dict[str, Any]:
    """Ask Jev with the owner's key, which Flight Deck holds. Validates here
    first so a malformed call never leaves the agent."""
    body = build_request(state, questions, model)
    return await _fd_call("POST", "/fd/jev/agent/ask", body, timeout)


async def models_via_flight_deck(*, timeout: float = _FD_TIMEOUT) -> list[dict[str, Any]]:
    data = await _fd_call("GET", "/fd/jev/agent/models", None, timeout)
    models = data.get("models")
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []
