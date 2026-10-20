"""File library — every file the caller's agents made, summarised and sorted.

The kiosk's "Library": one place for the files scattered across a user's
agents. Flight Deck walks each of the caller's process agents' workspaces on
disk (``<fd-data>/<slug>/data/workspace`` — the same tree the Files panel
merges in, so stopped agents count too), and for every new or changed file
reads its text (PDF / DOCX / XLSX / PPTX through the agent's own extractors)
and asks the owner's fast tier for a title, a short summary, one category
from a fixed list and a few tags. Results live in ``file_library.db`` beside
the agents' folders, keyed by agent + workspace path, and are re-summarised
only when a file's size or mtime changes.

Scans run in the background, one per user at a time, started when the
Library opens (or by its Rescan button); the list is searchable (filename,
title, summary, tags and the text itself) and filterable by agent, category
and kind. Tokens count toward the owner's daily allowance, and a scan stops
when it is spent. Workers Flight Deck runs itself (a run's agents, a being's
body) are not the user's library and are left out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from captain_claw.flight_deck.auth import get_current_user
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/fd/file-library", tags=["file-library"])

# The categories a file is sorted into: (id, label). The model picks exactly
# one; anything else lands in "other".
CATEGORIES: list[tuple[str, str]] = [
    ("report", "Report"),
    ("research", "Research"),
    ("notes", "Notes"),
    ("plan", "Plan"),
    ("data", "Data & lists"),
    ("finance", "Finance"),
    ("legal", "Legal"),
    ("correspondence", "Correspondence"),
    ("presentation", "Presentation"),
    ("code", "Code"),
    ("media", "Images & media"),
    ("other", "Other"),
]
_CATEGORY_IDS = {c for c, _ in CATEGORIES}

_KIND_BY_EXT: dict[str, str] = {}
for _kind, _exts in {
    "document": (".txt", ".md", ".markdown", ".rst", ".pdf", ".doc", ".docx", ".odt", ".rtf"),
    "spreadsheet": (".csv", ".tsv", ".xls", ".xlsx", ".ods"),
    "presentation": (".ppt", ".pptx", ".odp"),
    "web": (".html", ".htm"),
    "data": (".json", ".jsonl", ".yaml", ".yml", ".toml", ".xml"),
    "code": (".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".bash", ".sql", ".rb", ".go",
             ".rs", ".java", ".c", ".cpp", ".h", ".css", ".scss"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"),
    "media": (".mp3", ".wav", ".ogg", ".mp4", ".webm"),
    "archive": (".zip", ".tar", ".gz"),
    "config": (".env", ".ini", ".cfg", ".conf", ".log"),
}.items():
    for _e in _exts:
        _KIND_BY_EXT[_e] = _kind

KINDS: list[tuple[str, str]] = [
    ("document", "Documents"), ("spreadsheet", "Spreadsheets"), ("presentation", "Presentations"),
    ("web", "Web pages"), ("data", "Data files"), ("code", "Code"), ("image", "Images"),
    ("media", "Audio & video"), ("archive", "Archives"), ("config", "Config & logs"),
]

# Never descended into while walking a workspace (the agent's own list).
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              "dist", "build", ".datastore", ".history", ".reports"}
# Files per agent a walk lists at most — a runaway workspace can't stall a scan.
_MAX_FILES_PER_AGENT = 3000
# Files a single scan summarises at most; the next scan picks up the rest.
_MAX_SUMMARIES_PER_SCAN = 400
_CONCURRENCY = 3
# What the model reads of a file, and what search keeps of its text.
_LLM_CHARS = 7000
_EXCERPT_CHARS = 4000
_MAX_READ_BYTES = 400_000
_MAX_FILE_BYTES = 30 * 1024 * 1024
# Consecutive model failures after which a scan gives up (no key, no network).
_MAX_FAILURE_STREAK = 4
# A file whose summary failed is tried again by the scan that opening the
# library starts once this long has passed (Rescan retries it right away).
_ERROR_RETRY_SECONDS = 6 * 3600
_MAX_TOKENS = 700

# Text read straight from disk; the office formats go through the extractors.
_PLAIN_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".toml", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".sh", ".bash", ".sql", ".rb", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".css",
    ".scss", ".ini", ".cfg", ".conf", ".log", ".svg",
}
_EXTRACTED_EXTS = {".pdf", ".docx", ".xlsx", ".pptx"}
# Listed, never read: secrets, binaries no extractor opens.
_NOT_READ_REASON = {
    ".env": "Environment file — may hold secrets, so it is never read.",
}


def kind_of(ext: str) -> str:
    return _KIND_BY_EXT.get(ext.lower(), "other")


# ── Store ─────────────────────────────────────────────────────────────


def _data_dir() -> Path:
    from captain_claw.flight_deck.server import DATA_DIR
    return DATA_DIR


class LibraryStore:
    """``file_library.db``: one row per file, keyed by agent + workspace path."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS library_files (
                    id            TEXT PRIMARY KEY,
                    agent_slug    TEXT NOT NULL,
                    rel_path      TEXT NOT NULL,
                    filename      TEXT NOT NULL,
                    extension     TEXT NOT NULL DEFAULT '',
                    kind          TEXT NOT NULL DEFAULT 'other',
                    size          INTEGER NOT NULL DEFAULT 0,
                    modified      REAL NOT NULL DEFAULT 0,
                    fingerprint   TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL DEFAULT 'pending',
                    title         TEXT NOT NULL DEFAULT '',
                    summary       TEXT NOT NULL DEFAULT '',
                    category      TEXT NOT NULL DEFAULT '',
                    tags          TEXT NOT NULL DEFAULT '[]',
                    excerpt       TEXT NOT NULL DEFAULT '',
                    error         TEXT NOT NULL DEFAULT '',
                    model         TEXT NOT NULL DEFAULT '',
                    tokens        INTEGER NOT NULL DEFAULT 0,
                    summarized_at REAL,
                    added_at      REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_library_agent ON library_files(agent_slug, status);
                """
            )
            self._conn.commit()

    @staticmethod
    def file_id(slug: str, rel: str) -> str:
        return hashlib.sha1(f"{slug}\0{rel}".encode()).hexdigest()[:20]

    def sync_agent(self, slug: str, files: list[dict]) -> int:
        """Bring *slug*'s rows in line with what is on disk now: new files and
        changed ones (size/mtime) become pending, vanished ones go. Returns
        how many are pending for the agent afterwards."""
        now = time.time()
        with self._lock:
            have = {r["rel_path"]: r for r in self._conn.execute(
                "SELECT rel_path, fingerprint, status FROM library_files WHERE agent_slug = ?", (slug,))}
            seen: set[str] = set()
            for f in files:
                rel = f["rel"]
                seen.add(rel)
                old = have.get(rel)
                if old is None:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO library_files (id, agent_slug, rel_path, filename, extension, "
                        "kind, size, modified, fingerprint, status, added_at) VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)",
                        (self.file_id(slug, rel), slug, rel, f["filename"], f["extension"], f["kind"],
                         f["size"], f["modified"], f["fingerprint"], now))
                elif old["fingerprint"] != f["fingerprint"]:
                    # Changed: the old summary shows until the new one is in.
                    self._conn.execute(
                        "UPDATE library_files SET size = ?, modified = ?, fingerprint = ?, status = 'pending', "
                        "error = '' WHERE agent_slug = ? AND rel_path = ?",
                        (f["size"], f["modified"], f["fingerprint"], slug, rel))
            gone = [rel for rel in have if rel not in seen]
            for i in range(0, len(gone), 500):
                chunk = gone[i:i + 500]
                self._conn.execute(
                    f"DELETE FROM library_files WHERE agent_slug = ? AND rel_path IN ({','.join('?' * len(chunk))})",
                    (slug, *chunk))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT COUNT(*) FROM library_files WHERE agent_slug = ? AND status = 'pending'", (slug,)).fetchone()
            return int(row[0])

    def drop_agents_except(self, slugs: set[str]) -> None:
        """Forget the files of agents Flight Deck no longer has at all."""
        with self._lock:
            known = [r[0] for r in self._conn.execute("SELECT DISTINCT agent_slug FROM library_files")]
            for slug in known:
                if slug not in slugs:
                    self._conn.execute("DELETE FROM library_files WHERE agent_slug = ?", (slug,))
            self._conn.commit()

    def pending(self, slugs: list[str], limit: int, retry_errors_before: float) -> list[dict]:
        """Files waiting for a summary, newest first — with the failed ones
        last tried before *retry_errors_before*."""
        if not slugs:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM library_files WHERE agent_slug IN ({','.join('?' * len(slugs))}) "
                "AND (status = 'pending' OR (status = 'error' AND COALESCE(summarized_at, 0) < ?)) "
                "ORDER BY modified DESC LIMIT ?", (*slugs, retry_errors_before, limit)).fetchall()
        return [dict(r) for r in rows]

    def get(self, file_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM library_files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    def save_result(self, file_id: str, fingerprint: str, **fields: Any) -> None:
        """Store a summary — unless the file changed again meanwhile (its next
        scan summarises the new version)."""
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE library_files SET {cols} WHERE id = ? AND fingerprint = ?",
                (*fields.values(), file_id, fingerprint))
            self._conn.commit()

    def counts(self, slugs: list[str]) -> dict[str, int]:
        out = {"total": 0, "done": 0, "pending": 0, "error": 0, "skipped": 0}
        if not slugs:
            return out
        with self._lock:
            for status, n in self._conn.execute(
                    f"SELECT status, COUNT(*) FROM library_files WHERE agent_slug IN ({','.join('?' * len(slugs))}) "
                    "GROUP BY status", slugs):
                out[status] = out.get(status, 0) + int(n)
                out["total"] += int(n)
        return out

    def search(self, slugs: list[str], terms: list[str]) -> list[dict]:
        """Every row of these agents matching all *terms* (anywhere: name,
        title, summary, tags, category or the file's text). Matched here, not
        in SQL: SQLite's lower()/LIKE fold ASCII only, and the files aren't."""
        if not slugs:
            return []
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                f"SELECT * FROM library_files WHERE agent_slug IN ({','.join('?' * len(slugs))})", slugs)]
        if not terms:
            return rows
        fields = ("filename", "rel_path", "title", "summary", "tags", "category", "excerpt")
        return [r for r in rows
                if all(t in " ".join(str(r.get(k) or "") for k in fields).casefold() for t in terms)]


_STORE: LibraryStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> LibraryStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = LibraryStore(_data_dir() / "file_library.db")
        return _STORE


# ── Whose agents ──────────────────────────────────────────────────────


def _auth_enabled() -> bool:
    return os.environ.get("FD_AUTH_ENABLED", "true").lower() in ("true", "1", "yes")


def caller_agents(user: dict) -> dict[str, dict]:
    """The process agents whose files make up *user*'s library: their own
    (every agent, on a deck without sign-in), minus the workers Flight Deck
    runs itself. ``{slug: {name, running}}``."""
    from captain_claw.flight_deck.agent_sharing import is_managed_agent
    from captain_claw.flight_deck.server import _load_process_registry, _process_is_alive
    uid = str(user.get("id") or "")
    out: dict[str, dict] = {}
    for slug, entry in _load_process_registry().items():
        if _auth_enabled() and str(entry.get("owner") or "") != uid:
            continue
        if is_managed_agent(slug, str(entry.get("description") or "")):
            continue
        out[slug] = {
            "name": str(entry.get("name") or slug),
            "running": _process_is_alive(slug),
        }
    return out


def _workspace(slug: str) -> Path:
    return _data_dir() / slug / "data" / "workspace"


def walk_workspace(slug: str) -> list[dict]:
    """The agent's files on disk, as the Files panel lists them."""
    from captain_claw.flight_deck.server import _WORKSPACE_ALLOWED_EXTS
    root = _workspace(slug)
    out: list[dict] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            ext = Path(name).suffix.lower()
            if ext not in _WORKSPACE_ALLOWED_EXTS:
                continue
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "rel": p.relative_to(root).as_posix(),
                "filename": name,
                "extension": ext,
                "kind": kind_of(ext),
                "size": int(st.st_size),
                "modified": float(st.st_mtime),
                "fingerprint": f"{st.st_size}:{int(st.st_mtime)}",
            })
            if len(out) >= _MAX_FILES_PER_AGENT:
                return out
    return out


def _resolve_path(row: dict) -> Path | None:
    """The row's file on disk — only ever inside its agent's workspace."""
    from captain_claw.vfs import safe_join
    root = _workspace(row["agent_slug"])
    p = safe_join(root, row["rel_path"])
    return p if p is not None and p.is_file() else None


# ── Reading a file ────────────────────────────────────────────────────


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def _squash(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def read_text(path: Path, ext: str) -> tuple[str, str]:
    """(text, why-not): the file's text for the model, or "" and the reason
    it can't be read."""
    if ext in _NOT_READ_REASON:
        return "", _NOT_READ_REASON[ext]
    if ext in _PLAIN_TEXT_EXTS:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_READ_BYTES)
        text = raw.decode("utf-8", errors="replace")
        if ext in (".html", ".htm", ".svg"):
            text = _TAG_RE.sub(" ", text)
        return _squash(text), ""
    if ext in _EXTRACTED_EXTS:
        from captain_claw.tools import document_extract as dx
        if ext == ".pdf":
            text, err = dx._extract_pdf_markdown(path, 10)
            if err:
                return "", err
            text = text or ""
        elif ext == ".docx":
            text = dx._extract_docx_markdown(path)
        elif ext == ".xlsx":
            text = dx._extract_xlsx_markdown(path, 60)
        else:
            text = dx._extract_pptx_markdown(path, 30)
        text = _squash(text)
        if "No extractable text found" in text and len(text) < 300:
            return "", "No text could be read from it (a scan or an image-only file)."
        return text, ""
    return "", ""


def _humanize(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[-_]+", " ", stem)
    stem = re.sub(r"\s*\b\d{8}[- ]?\d{4,6}\b", "", stem).strip()
    return (stem[:1].upper() + stem[1:]) if stem else filename


def _metadata_only(row: dict, reason: str = "") -> dict:
    """A file the model doesn't read: titled from its name, sorted by kind."""
    kind = row["kind"]
    category = "media" if kind in ("image", "media") else "code" if kind == "code" else "other"
    default = {
        "image": "Image — nothing to read, so not summarised.",
        "media": "Audio or video — not summarised.",
        "archive": "Archive — not summarised.",
    }.get(kind, "This file type can't be read for a summary.")
    return {
        "status": "skipped",
        "title": _humanize(row["filename"]),
        "summary": reason or default,
        "category": category,
        "tags": "[]",
        "excerpt": "",
        "error": "",
        "model": "",
        "tokens": 0,
        "summarized_at": time.time(),
    }


# ── The model ─────────────────────────────────────────────────────────


_SYSTEM = """You catalogue files that AI agents produced for the person they work for.
Read the file and reply with ONLY a JSON object, no prose, no code fence:
{"title": "...", "summary": "...", "category": "...", "tags": ["...", "..."]}

- title: a clear human title for the file, at most 80 characters.
- summary: 2-3 sentences, at most 420 characters: what the file is and its key
  takeaways (names, numbers, conclusions). No "This file..." filler.
- category: exactly one of: %s
- tags: 2-5 short lowercase tags (topics, companies, people, places).
Write the title and summary in the language the file is written in."""


async def _resolve_tier(user_id: str) -> dict | None:
    """The owner's fast tier (else balanced, else any): the model that
    writes their library. None when they have no tier set at all."""
    from captain_claw.flight_deck.basna_routes import _load_owner_tiers
    tiers: dict = {}
    try:
        from captain_claw.flight_deck.auth import get_db
        tiers, _env = await _load_owner_tiers(get_db(), user_id)
    except Exception:
        tiers = {}
    if not tiers:
        try:
            from captain_claw.flight_deck.basna_routes import _load_registry
            tiers = _load_registry().get("tiers") or {}
        except Exception:
            tiers = {}
    cfg = tiers.get("fast") or tiers.get("balanced") or next(iter(tiers.values()), None)
    return cfg if isinstance(cfg, dict) and cfg.get("model") else None


def _parse_reply(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("the model did not answer with JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("the model did not answer with a JSON object")
    category = str(data.get("category") or "").strip().lower()
    tags_raw = data.get("tags") if isinstance(data.get("tags"), list) else []
    tags: list[str] = []
    for t in tags_raw:
        t = " ".join(str(t).lower().split())[:32]
        if t and t not in tags:
            tags.append(t)
    return {
        "title": " ".join(str(data.get("title") or "").split())[:120],
        "summary": " ".join(str(data.get("summary") or "").split())[:600],
        "category": category if category in _CATEGORY_IDS else "other",
        "tags": tags[:5],
    }


async def summarize_row(row: dict, tier: dict, owner_id: str) -> dict:
    """Read one file and have the model catalogue it. Returns the columns to
    store (never raises for a file it can't read)."""
    path = _resolve_path(row)
    if path is None:
        return {"status": "error", "error": "The file is gone.", "summarized_at": time.time()}
    if row["size"] == 0:
        return _metadata_only(row, "Empty file.")
    if row["size"] > _MAX_FILE_BYTES and row["extension"] not in _PLAIN_TEXT_EXTS:
        return _metadata_only(row, "Too large to read here.")
    try:
        text, why_not = await asyncio.to_thread(read_text, path, row["extension"])
    except Exception as e:  # noqa: BLE001 — a broken file is a skipped file
        return _metadata_only(row, f"Could not be read: {str(e)[:160]}")
    if not text:
        return _metadata_only(row, why_not)

    from captain_claw.flight_deck.basna_routes import tier_api_key
    from captain_claw.llm import Message, create_provider
    body = text if len(text) <= _LLM_CHARS else text[:_LLM_CHARS] + "\n\n[… truncated]"
    user_msg = (f"File: {row['rel_path']}\nAgent: {row.get('agent_name') or row['agent_slug']}\n"
                f"Size: {row['size']} bytes\n\n---\n{body}")
    provider = create_provider(
        provider=tier.get("provider", "anthropic"), model=tier.get("model", ""),
        base_url=tier.get("base_url") or None, api_key=tier_api_key(tier),
        temperature=0.2, max_tokens=_MAX_TOKENS)
    resp = await provider.complete(
        messages=[Message(role="system", content=_SYSTEM % ", ".join(c for c, _ in CATEGORIES)),
                  Message(role="user", content=user_msg)],
        temperature=0.2, max_tokens=_MAX_TOKENS)
    usage = dict(getattr(resp, "usage", None) or {})
    tokens = int(usage.get("total_tokens") or 0) or (
        int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0))
    if owner_id and _auth_enabled() and tokens:
        try:
            from captain_claw.flight_deck import usage_quota
            from captain_claw.flight_deck.auth import get_db
            await usage_quota.record_tokens(get_db(), owner_id, tokens)
        except Exception:
            log.debug("file library: token report failed", exc_info=True)
    parsed = _parse_reply(resp.content or "")
    return {
        "status": "done",
        "title": parsed["title"] or _humanize(row["filename"]),
        "summary": parsed["summary"],
        "category": parsed["category"],
        "tags": json.dumps(parsed["tags"], ensure_ascii=False),
        "excerpt": text[:_EXCERPT_CHARS],
        "error": "",
        "model": str(tier.get("model") or ""),
        "tokens": tokens,
        "summarized_at": time.time(),
    }


# ── Scans ─────────────────────────────────────────────────────────────


@dataclass
class ScanJob:
    running: bool = False
    phase: str = "idle"          # idle | scanning | summarizing
    total: int = 0               # files this scan set out to summarise
    done: int = 0
    failed: int = 0
    current: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    message: str = ""            # why it stopped early, if it did
    task: asyncio.Task | None = field(default=None, repr=False)

    def view(self) -> dict:
        return {
            "running": self.running, "phase": self.phase, "total": self.total,
            "done": self.done, "failed": self.failed, "current": self.current,
            "started_at": self.started_at or None, "finished_at": self.finished_at or None,
            "message": self.message,
        }


_JOBS: dict[str, ScanJob] = {}


def _job_for(uid: str) -> ScanJob:
    return _JOBS.setdefault(uid, ScanJob())


async def _quota_spent(owner_id: str) -> bool:
    if not (owner_id and _auth_enabled()):
        return False
    try:
        from captain_claw.flight_deck import usage_quota
        return await usage_quota.automation_blocked(owner_id) is not None
    except Exception:
        return False


async def run_scan(user: dict, job: ScanJob, retry_errors: bool = False) -> None:
    uid = str(user.get("id") or "")
    store = get_store()
    try:
        agents = caller_agents(user)
        # Forget removed agents only on a registry read that worked: the
        # lenient reader returns {} on a failed read, which would drop them all.
        from captain_claw.flight_deck.server import _load_process_registry_strict
        try:
            store.drop_agents_except(set(_load_process_registry_strict().keys()))
        except (OSError, ValueError):
            pass
        for slug in agents:
            files = await asyncio.to_thread(walk_workspace, slug)
            store.sync_agent(slug, files)
        todo = store.pending(list(agents), _MAX_SUMMARIES_PER_SCAN,
                             time.time() if retry_errors else time.time() - _ERROR_RETRY_SECONDS)
        if not todo:
            return
        tier = await _resolve_tier(uid)
        job.phase, job.total = "summarizing", len(todo)
        if tier is None:
            job.message = "No model is set up to write summaries — add one in your model settings."
            return
        if await _quota_spent(uid):
            job.message = "Today's token allowance is used up — summaries continue tomorrow."
            return

        sem = asyncio.Semaphore(_CONCURRENCY)
        streak = 0
        stop = asyncio.Event()

        async def one(row: dict) -> None:
            nonlocal streak
            async with sem:
                if stop.is_set():
                    return
                if await _quota_spent(uid):
                    job.message = "Today's token allowance ran out — the rest continue tomorrow."
                    stop.set()
                    return
                row["agent_name"] = agents.get(row["agent_slug"], {}).get("name", row["agent_slug"])
                job.current = row["filename"]
                try:
                    result = await summarize_row(row, tier, uid)
                    streak = 0
                except Exception as e:  # noqa: BLE001 — recorded on the row
                    streak += 1
                    job.failed += 1
                    store.save_result(row["id"], row["fingerprint"], status="error",
                                      error=str(e)[:300], summarized_at=time.time())
                    if streak >= _MAX_FAILURE_STREAK:
                        job.message = f"Stopped: the model keeps failing ({str(e)[:160]})."
                        stop.set()
                    return
                store.save_result(row["id"], row["fingerprint"], **result)
                job.done += 1

        await asyncio.gather(*(one(r) for r in todo))
    except Exception as e:  # noqa: BLE001 — a scan never takes FD down
        log.warning("file library scan failed", user=uid, error=str(e))
        job.message = f"The scan failed: {str(e)[:200]}"
    finally:
        job.running, job.phase, job.current = False, "idle", ""
        job.finished_at = time.time()
        job.task = None


def start_scan(user: dict, retry_errors: bool = False) -> ScanJob:
    uid = str(user.get("id") or "")
    job = _job_for(uid)
    if job.running:
        return job
    job.running, job.phase = True, "scanning"
    job.total = job.done = job.failed = 0
    job.current = job.message = ""
    job.started_at, job.finished_at = time.time(), 0.0
    job.task = asyncio.create_task(run_scan(user, job, retry_errors))
    return job


# ── Rows → what the UI shows ──────────────────────────────────────────


def _item(row: dict, agents: dict[str, dict], terms: list[str] | None = None) -> dict:
    try:
        tags = json.loads(row.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    mt, _ = mimetypes.guess_type(row["filename"])
    item = {
        "id": row["id"],
        "agent_slug": row["agent_slug"],
        "agent_name": agents.get(row["agent_slug"], {}).get("name", row["agent_slug"]),
        "agent_running": bool(agents.get(row["agent_slug"], {}).get("running")),
        "rel_path": row["rel_path"],
        "filename": row["filename"],
        "extension": row["extension"],
        "kind": row["kind"],
        "mime_type": mt or "application/octet-stream",
        "size": row["size"],
        "modified": row["modified"],
        "status": row["status"],
        "title": row["title"],
        "summary": row["summary"],
        "category": row["category"],
        "tags": tags if isinstance(tags, list) else [],
        "error": row["error"],
        "model": row["model"],
        "summarized_at": row["summarized_at"],
        "added_at": row["added_at"],
        "snippet": "",
    }
    if terms:
        item["snippet"] = _snippet(row, terms)
    return item


def _snippet(row: dict, terms: list[str]) -> str:
    """A line of the file's text around the first term the visible fields
    don't already show — why a search found it."""
    visible = " ".join(str(row.get(k) or "") for k in ("filename", "rel_path", "title", "summary", "tags", "category")).casefold()
    text = row.get("excerpt") or ""
    low = text.casefold()
    for t in terms:
        if t in visible:
            continue
        i = low.find(t)
        if i < 0:
            continue
        a, b = max(0, i - 70), min(len(text), i + len(t) + 110)
        return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")
    return ""


_SORTS = {
    "recent": lambda r: -float(r["modified"] or 0),
    "name": lambda r: (r["title"] or r["filename"]).lower(),
    "size": lambda r: -int(r["size"] or 0),
    "agent": lambda r: (r["agent_slug"], -float(r["modified"] or 0)),
}


# ── Routes ────────────────────────────────────────────────────────────


def _status_payload(user: dict) -> dict:
    agents = caller_agents(user)
    job = _job_for(str(user.get("id") or ""))
    return {"job": job.view(), "counts": get_store().counts(list(agents)), "agents": len(agents)}


@router.get("/status")
async def library_status(user: dict = Depends(get_current_user)) -> dict:
    """Where the caller's library stands: file counts and the running scan."""
    return _status_payload(user)


@router.post("/scan")
async def library_scan(retry_errors: bool = False, user: dict = Depends(get_current_user)) -> dict:
    """Look for new and changed files and summarise them, in the background
    (``retry_errors``: the files whose summary failed too, now). A scan
    already running is left to finish."""
    start_scan(user, retry_errors)
    return _status_payload(user)


@router.get("/files")
async def library_files(
    q: str = "", agent: str = "", category: str = "", kind: str = "",
    sort: str = "recent", limit: int = 60, offset: int = 0,
    user: dict = Depends(get_current_user),
) -> dict:
    """The caller's files, searched and filtered, with the counts each
    filter would leave (computed under the other filters)."""
    agents = caller_agents(user)
    terms = [t for t in q.casefold().split() if t][:8]
    rows = get_store().search(list(agents), terms)

    def keep(r: dict, skip: str = "") -> bool:
        return ((skip == "agent" or not agent or r["agent_slug"] == agent)
                and (skip == "category" or not category or (r["category"] or "pending") == category)
                and (skip == "kind" or not kind or r["kind"] == kind))

    def facet(key: str, field_of) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            if keep(r, key):
                k = field_of(r)
                out[k] = out.get(k, 0) + 1
        return out

    agent_counts = facet("agent", lambda r: r["agent_slug"])
    category_counts = facet("category", lambda r: r["category"] or "pending")
    kind_counts = facet("kind", lambda r: r["kind"])
    shown = [r for r in rows if keep(r)]
    shown.sort(key=_SORTS.get(sort, _SORTS["recent"]))
    limit = max(1, min(limit, 1000))
    page = shown[max(0, offset):max(0, offset) + limit]
    return {
        "items": [_item(r, agents, terms) for r in page],
        "total": len(shown),
        "facets": {
            "agents": sorted(
                ({"id": s, "label": agents.get(s, {}).get("name", s), "count": n} for s, n in agent_counts.items()),
                key=lambda x: (-x["count"], x["label"].lower())),
            "categories": [{"id": c, "label": label, "count": category_counts[c]}
                           for c, label in [*CATEGORIES, ("pending", "Not summarised yet")]
                           if category_counts.get(c)],
            "kinds": [{"id": k, "label": label, "count": kind_counts[k]}
                      for k, label in [*KINDS, ("other", "Other")] if kind_counts.get(k)],
        },
    }


def _owned_row(file_id: str, user: dict) -> tuple[dict, dict[str, dict]]:
    row = get_store().get(file_id)
    agents = caller_agents(user)
    if row is None or row["agent_slug"] not in agents:
        raise HTTPException(404, "file not found")
    return row, agents


@router.post("/files/{file_id}/summarize")
async def library_resummarize(file_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Summarise one file again, now."""
    row, agents = _owned_row(file_id, user)
    uid = str(user.get("id") or "")
    if await _quota_spent(uid):
        raise HTTPException(429, "Today's token allowance is used up.")
    tier = await _resolve_tier(uid)
    if tier is None:
        raise HTTPException(409, "No model is set up to write summaries.")
    row["agent_name"] = agents[row["agent_slug"]]["name"]
    try:
        result = await summarize_row(row, tier, uid)
    except Exception as e:  # noqa: BLE001
        get_store().save_result(row["id"], row["fingerprint"], status="error",
                                error=str(e)[:300], summarized_at=time.time())
        raise HTTPException(502, f"The model failed: {str(e)[:200]}")
    get_store().save_result(row["id"], row["fingerprint"], **result)
    return _item(get_store().get(file_id) or row, agents)


def _serve(file_id: str, user: dict, inline: bool):
    row, _agents = _owned_row(file_id, user)
    path = _resolve_path(row)
    if path is None:
        raise HTTPException(404, "file not found")
    mt, _ = mimetypes.guess_type(row["filename"])
    headers = {"X-Content-Type-Options": "nosniff"}
    if inline:
        from captain_claw.web import active_content
        if active_content.active_view_type(mt or ""):
            headers["Content-Security-Policy"] = active_content.ACTIVE_VIEW_CSP
    return FileResponse(
        path, media_type=mt or "application/octet-stream", headers=headers,
        filename=row["filename"], content_disposition_type="inline" if inline else "attachment")


@router.get("/files/{file_id}/view")
async def library_view(file_id: str, user: dict = Depends(get_current_user)):
    """The file itself, inline — straight from the agent's folder on disk, so
    it opens whether the agent is running or not."""
    return _serve(file_id, user, inline=True)


@router.get("/files/{file_id}/download")
async def library_download(file_id: str, user: dict = Depends(get_current_user)):
    return _serve(file_id, user, inline=False)
