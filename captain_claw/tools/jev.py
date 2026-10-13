"""Jev (TypeSafe AI) tool — fast, calibrated judgments about text.

Jev answers typed questions about a ``state`` (text, a JSON object or an
array) with probabilities; it never writes text (:mod:`captain_claw.jev_client`
has the API, the validation and the answer formatting):

* ``ask``    — one call, any number of questions about the same state.
* ``models`` — the models the key can use.

Where the key lives decides the path:

* Under Flight Deck the owner's key stays in Flight Deck (Connections → Jev):
  the tool asks ``/fd/jev/agent/*`` and Flight Deck calls Jev with the owner's
  key. Owner-only — a shared-agent member's turn is refused here, before any
  file is read or request made (``/fd/jev/*`` is not grant-aware either).
* Standalone the agent calls Jev itself with ``tools.jev.api_key`` /
  ``JEV_API_KEY`` / ``TYPESAFE_API_KEY``.

The registry hides the tool (metadata ``requires_jev``) until
:func:`jev_client.jev_usable` says a key is there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from captain_claw import jev_client
from captain_claw.config import get_config
from captain_claw.tools.registry import Tool, ToolResult


def _given(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


# Formats that are never text, whatever their bytes look like.
_BINARY_SUFFIXES = frozenset({
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".ods",
    ".zip", ".gz", ".tar", ".7z", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".heic", ".bmp", ".tiff", ".mp3", ".m4a", ".wav", ".ogg", ".mp4", ".mov",
})
_EXTRACT_HINT = (
    "turn it into text first (pdf_extract / docx_extract / xlsx_extract / "
    "pptx_extract), then pass that text or a .txt / .md file"
)


def _read_state_file(raw: Any, runtime: dict[str, Any]) -> str:
    """The text of the file *raw* names, found the way the read tool finds it
    (``vfs:`` paths, absolute, the workspace, ``saved/``, the workflow run
    folder, then the file registry)."""
    from captain_claw.mail_attachments import resolve_path

    value = str(raw).strip()
    path = resolve_path(value, runtime)
    if path is None:
        raise jev_client.JevInputError(f"state_file not found: {value}")
    if path.suffix.lower() in _BINARY_SUFFIXES:
        raise jev_client.JevInputError(f"state_file {path.name} isn't text — {_EXTRACT_HINT}")
    # A UTF-8 character is at most 4 bytes, so anything bigger can't fit.
    limit = jev_client.MAX_STATE_CHARS * 4
    try:
        size = path.stat().st_size
        if size > limit:
            raise jev_client.JevInputError(
                f"state_file is {size:,} bytes; Jev reads about "
                f"{jev_client.MAX_STATE_CHARS:,} characters (~30k tokens). "
                "Send only the part the questions need."
            )
        data = path.read_bytes()
    except OSError as exc:
        raise jev_client.JevInputError(
            f"Could not read state_file {path}: {exc.strerror or type(exc).__name__}"
        ) from None
    text = data.decode("utf-8", errors="replace")
    if b"\x00" in data[:8192] or text.count("\ufffd") > max(32, len(text) // 20):
        raise jev_client.JevInputError(f"state_file {path.name} looks binary, not text — {_EXTRACT_HINT}")
    return text


class JevTool(Tool):
    """Ask Jev typed questions about a piece of text."""

    name = "jev"
    description = (
        "Jev (TypeSafe AI): fast, calibrated JUDGMENTS about text — it never writes text. Give it a "
        "state (the content: text, or JSON object/array text) and named questions; each comes back "
        "with probabilities. Question types: noul = yes/no → p(yes) (yes_means / no_means sharpen "
        "it); choice = pick one of 2–255 options → the choice, per-option probabilities and a "
        "confidence (give every option a description); score = a level on an ordered rubric of 2–10 "
        "levels, lowest first → expected level and confidence. Ask SEVERAL questions about the same "
        "state in ONE call. Good for triage, routing, classification, relevance filtering, checking "
        "a claim against a source, rating against a rubric, deciding whether something needs "
        "attention. Write literal, specific questions (Jev reads them literally; English works "
        "best) and keep the state to what the questions need (about 30k tokens at most; long text "
        "goes in state_file). Not for math, counting, date arithmetic or writing text — do those "
        "yourself. action=models lists the models."
    )
    # Flight Deck in front of Jev, plus Jev's own retries on 429 / 529.
    timeout_seconds = 150.0
    # Provider-friendly schema: no additionalProperties maps, no type unions.
    # Never a parameter named ``timeout`` — the registry reads that as the
    # call's own timeout.
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["ask", "models"],
                "description": "ask (default): answer the questions about the state. models: list Jev's models.",
            },
            "state": {
                "type": "string",
                "description": (
                    "The content the questions are about — text, or a JSON object/array as text "
                    "(sent structured). Only what the questions need."
                ),
            },
            "state_file": {
                "type": "string",
                "description": (
                    "Instead of state: a text file to use as the state (a path as for read, e.g. "
                    "absolute, workspace-relative or vfs:). Not together with state. PDFs/Office "
                    "files: extract their text first."
                ),
            },
            "questions": {
                "type": "array",
                "description": "One or more questions about the same state, each with a unique name.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short unique id for the answer, e.g. is_urgent, topic, quality.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["noul", "choice", "score"],
                            "description": "noul = yes/no; choice = one of the options; score = a rubric level.",
                        },
                        "instructions": {
                            "type": "string",
                            "description": "The question itself, literal and specific.",
                        },
                        "options": {
                            "type": "array",
                            "description": "choice: 2–255 options, each a name plus what it means.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "Option name (what comes back)."},
                                    "description": {"type": "string", "description": "When this option applies."},
                                },
                                "required": ["name"],
                            },
                        },
                        "levels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "score: 2–10 level descriptions, LOWEST first.",
                        },
                        "yes_means": {"type": "string", "description": "noul (optional): what counts as yes."},
                        "no_means": {"type": "string", "description": "noul (optional): what counts as no."},
                    },
                    "required": ["name", "type", "instructions"],
                },
            },
            "model": {
                "type": "string",
                "description": "Jev model (default from config, normally jev-latest).",
            },
        },
    }

    async def execute(self, action: str = "ask", **kwargs: Any) -> ToolResult:
        runtime = {k: kwargs.pop(k) for k in [k for k in kwargs if str(k).startswith("_")]}
        action = str(action or "ask").strip().lower() or "ask"
        if action not in ("ask", "models"):
            return ToolResult(success=False, error=f"Unknown jev action '{action}' — use ask or models.")
        # Owner-only: a member's turn never reaches the owner's key, nor reads
        # a file for it.
        if jev_client._member_call():
            return ToolResult(success=False, error=jev_client.MEMBER_REFUSED)
        under_fd = jev_client.under_flight_deck()
        if not under_fd and not jev_client.standalone_api_key():
            return ToolResult(success=False, error=jev_client.NO_KEY_STANDALONE)
        try:
            if action == "models":
                if under_fd:
                    models = await jev_client.models_via_flight_deck()
                else:
                    models = await jev_client.standalone_api().models()
                return ToolResult(success=True, content=jev_client.format_models(models))

            state = kwargs.get("state")
            state_file = kwargs.get("state_file")
            if _given(state) and _given(state_file):
                return ToolResult(
                    success=False,
                    error="Give either state or state_file, not both.",
                )
            if _given(state_file):
                state = _read_state_file(state_file, runtime)
            questions = kwargs.get("questions")
            if questions is None:
                raise jev_client.JevInputError("questions are required — at least one question")
            model = str(kwargs.get("model") or "").strip() or self._default_model()
            if under_fd:
                resp = await jev_client.ask_via_flight_deck(state, questions, model)
            else:
                resp = await jev_client.standalone_api().ask(state, questions, model)
        except (jev_client.JevInputError, jev_client.JevError) as exc:
            return ToolResult(success=False, error=str(exc))
        return ToolResult(success=True, content="Jev answers:\n" + jev_client.format_answers(resp))

    @staticmethod
    def _default_model() -> str:
        try:
            cfg = get_config().tools.jev
            return str(getattr(cfg, "model", "") or "").strip() or jev_client.DEFAULT_MODEL
        except Exception:
            return jev_client.DEFAULT_MODEL
