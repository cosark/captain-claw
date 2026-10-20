"""Persistent storage for Flight-Deck-managed MCP server configurations.

A flat JSON file containing a list of server records. Its location is
per-instance — see :func:`_storage_path` — so a host running several
Flight Decks does not leak one deck's MCP servers into another; only the
legacy ``~/.captain-claw-fd/mcp_servers.json`` fallback is shared, used
when no isolation env (``CAPTAIN_CLAW_FD_MCP_PATH`` / ``CAPTAIN_CLAW_FD_HOME``
/ ``FD_DATA_DIR``) is set. Anything fancier (SQLite, encryption-at-rest)
is deferred until we have a clearer picture of what users actually want.

Within one deck a server is **private to the user who added it** — only
that user's agents may use it, and only that user sees, calls or manages
it — unless an admin made it ``deck_wide`` (every user's agents, subject to
``allowed_agents``; only admins manage it). See :func:`agent_may_use`,
:func:`user_may_use` and :func:`user_may_manage`. With Flight Deck auth off
there is one trusted user and ownership is not enforced.

Schema (one element per configured server)::

    {
        "name":          "examplemcp",                     # unique key
        "transport":     "http",                          # "http" (default) or "stdio"
        "url":           "https://example.com/mcp",       # http: streamable-http endpoint
        "command":       "uvx",                           # stdio: executable to launch
        "args":          ["mcp-server-foo", "--flag"],    # stdio: argv after command
        "env":           {"PATH": "/usr/bin"},            # stdio: extra env vars
        "auth":          "",                              # "" (none / client_credentials) or "oauth" (sign-in, DCR)
        "client_id":     "abc",                           # optional OAuth2 client_id (http only)
        "client_secret": "xyz",                           # optional OAuth2 client_secret (http only)
        "token_endpoint": "/oauth/token",                 # optional, abs or rel (client_credentials only)
        "scope":         "read write",                    # optional scope to ask for (oauth only)
        "headers":       {"X-Foo": "bar"},                # optional extra HTTP headers (http only)
        "enabled":       true,                            # disabled servers are skipped
        "allowed_agents": ["nano-man", "everyday"],       # empty list = all agents allowed
        "owner_id":      "user-uuid",                     # who added it ("" = nobody, see below)
        "deck_wide":     false,                           # admin opt-in: every user's agents
        "added_at":      1715000000.0                     # unix seconds
    }

The file is rewritten atomically (write to ``*.tmp`` then ``rename``)
so a crashed FD never leaves callers with a half-written JSON.

The module exposes a tiny CRUD API so the routes layer doesn't need to
know about file locations or atomic writes:

* :func:`load_servers` — return the list (empty when no file)
* :func:`save_servers` — persist a list, overwriting prior contents
* :func:`get_server` / :func:`upsert_server` / :func:`delete_server` —
  by-name conveniences

A process-wide :class:`asyncio.Lock` serialises writes so concurrent
admin requests can't clobber each other.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from captain_claw.flight_deck.auth import _fd_auth_enabled
from captain_claw.logging import get_logger

log = get_logger(__name__)


_DEFAULT_DIR = Path.home() / ".captain-claw-fd"
_DEFAULT_PATH = _DEFAULT_DIR / "mcp_servers.json"


def _storage_path() -> Path:
    """Resolve this instance's MCP servers file.

    Precedence (first match wins) so a host running several Flight Decks
    isolates MCP config the same way it already isolates the rest of its
    state — the shared legacy default was the source of cross-instance
    leaks (one deck's servers showing up in another):

    1. ``CAPTAIN_CLAW_FD_MCP_PATH`` — an explicit file, always wins.
    2. ``CAPTAIN_CLAW_FD_HOME`` — the FD home dir that also holds
       ``agent_secret`` / ``apps`` / ``app_data``; keep the file beside them.
    3. ``FD_DATA_DIR`` — the general per-instance data dir (DB, plans, …).
    4. the legacy shared ``~/.captain-claw-fd/mcp_servers.json``.

    Only #4 is shared across instances; setting any of #1–#3 (as every
    isolated deck already does for its data dir) gives this deck its own
    MCP config, so a new install starts with no servers instead of
    inheriting whatever another deck configured.
    """
    override = os.environ.get("CAPTAIN_CLAW_FD_MCP_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    home = os.environ.get("CAPTAIN_CLAW_FD_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve() / "mcp_servers.json"
    data_dir = os.environ.get("FD_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).expanduser().resolve() / "mcp_servers.json"
    return _DEFAULT_PATH


# One-time migration hint: warn (once per resolved path) when an isolated
# deck finds no MCP file of its own but the legacy shared one exists — so an
# admin who *wants* those servers here knows to copy the file in, while a
# deck that should stay clean (e.g. a kiosk) silently gets no servers.
_migration_warned: set[str] = set()


def _maybe_warn_legacy_migration(path: Path) -> None:
    if path == _DEFAULT_PATH:
        return
    key = str(path)
    if key in _migration_warned:
        return
    _migration_warned.add(key)
    try:
        if not path.exists() and _DEFAULT_PATH.exists():
            log.info(
                "No MCP servers file at %s; the legacy shared file %s exists but "
                "is no longer read by an isolated instance. Copy it here if this "
                "deck should keep those servers, or leave it for a clean deck.",
                path,
                _DEFAULT_PATH,
            )
    except Exception:
        pass


_write_lock = asyncio.Lock()


# ── helpers ─────────────────────────────────────────────────────────


def _coerce_record(raw: Any) -> dict[str, Any] | None:
    """Validate a record loaded from disk; drop entries we can't parse.

    Two transport flavours are accepted:

    * ``transport == "http"`` (default) — requires ``url``.
    * ``transport == "stdio"`` — requires ``command``; ``url`` is ignored.

    Records without the matching transport-specific field are dropped.
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    transport = str(raw.get("transport") or "http").strip().lower()
    if transport not in ("http", "stdio"):
        transport = "http"

    url = str(raw.get("url") or "").strip()
    command = str(raw.get("command") or "").strip()

    if transport == "http" and not url:
        return None
    if transport == "stdio" and not command:
        return None

    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    args = raw.get("args") or []
    if not isinstance(args, list):
        args = []
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        env = {}
    allowed = raw.get("allowed_agents") or []
    if not isinstance(allowed, list):
        allowed = []
    allowed = [str(s).strip() for s in allowed if str(s).strip()]
    # "oauth" = interactive sign-in (mcp_client_oauth); anything else keeps the
    # original behaviour (client_credentials when client_id + token_endpoint).
    auth = "oauth" if transport == "http" and str(raw.get("auth") or "").strip().lower() == "oauth" else ""
    # Records saved before servers had owners carry no ``owner_id`` key: they
    # were deck-wide, and stay so (migrate_legacy_ownership persists that) —
    # except an OAuth sign-in, which is one person's account: private, and
    # unusable until the migration finds who signed in (fail closed).
    if "owner_id" in raw:
        deck_wide = bool(raw.get("deck_wide", False))
    else:
        deck_wide = auth != "oauth"

    return {
        "name": name,
        "transport": transport,
        "url": url,
        "command": command,
        "args": [str(a) for a in args],
        "env": {str(k): str(v) for k, v in env.items()},
        "auth": auth,
        "scope": str(raw.get("scope") or "").strip(),
        "client_id": str(raw.get("client_id") or ""),
        "client_secret": str(raw.get("client_secret") or ""),
        "token_endpoint": str(raw.get("token_endpoint") or ""),
        "headers": {str(k): str(v) for k, v in headers.items()},
        "enabled": bool(raw.get("enabled", True)),
        "allowed_agents": allowed,
        "owner_id": str(raw.get("owner_id") or "").strip(),
        "deck_wide": deck_wide,
        "added_at": float(raw.get("added_at") or 0.0),
    }


def _public_view(record: dict[str, Any]) -> dict[str, Any]:
    """Return a record with secrets masked for UI consumption."""
    out = dict(record)
    secret = out.get("client_secret") or ""
    if secret:
        out["client_secret"] = "•" * 8  # placeholder
        out["client_secret_set"] = True
    else:
        out["client_secret"] = ""
        out["client_secret_set"] = False
    return out


# ── load / save ─────────────────────────────────────────────────────


def load_servers() -> list[dict[str, Any]]:
    path = _storage_path()
    if not path.exists():
        _maybe_warn_legacy_migration(path)
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to read MCP servers file %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        log.warning("MCP servers file %s did not contain a list; ignoring", path)
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        rec = _coerce_record(entry)
        if rec is not None:
            out.append(rec)
    return out


async def save_servers(records: list[dict[str, Any]]) -> None:
    """Atomically persist ``records``. Caller is expected to validate
    each record (it must already be in canonical shape)."""
    path = _storage_path()
    async with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        body = json.dumps(records, indent=2, sort_keys=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)


# ── by-name conveniences ────────────────────────────────────────────


def get_server(name: str) -> dict[str, Any] | None:
    name = name.strip()
    if not name:
        return None
    for rec in load_servers():
        if rec["name"] == name:
            return rec
    return None


def is_agent_allowed(record: dict[str, Any], agent_slug: str) -> bool:
    """Return ``True`` when ``agent_slug`` is allowed to use ``record``.

    The allow-list is opt-in: an empty ``allowed_agents`` list means
    "every agent is allowed" (the Phase 1 behaviour). Once any slug is
    listed, only those slugs may use the server.

    A ``""`` slug (caller couldn't determine its identity) is allowed
    only when ``allowed_agents`` is empty — gating on identity requires
    *having* an identity.
    """
    allowed = record.get("allowed_agents") or []
    if not allowed:
        return True
    if not agent_slug:
        return False
    return agent_slug in allowed


def _is_admin(user: dict[str, Any] | None) -> bool:
    return (user or {}).get("role") == "admin"


def agent_may_use(record: dict[str, Any], agent_slug: str, agent_owner: str) -> bool:
    """May the agent (slug + the owner FD recorded at spawn) use ``record``?
    A private server admits only its owner's agents; then the allowlist."""
    if not is_agent_allowed(record, agent_slug):
        return False
    if not _fd_auth_enabled() or record.get("deck_wide"):
        return True
    owner = str(record.get("owner_id") or "")
    return bool(owner) and agent_owner == owner


def user_may_use(record: dict[str, Any], user: dict[str, Any] | None) -> bool:
    """May this signed-in user see ``record`` and call its tools?"""
    if not _fd_auth_enabled() or record.get("deck_wide"):
        return True
    owner = str(record.get("owner_id") or "")
    return bool(owner) and str((user or {}).get("id") or "") == owner


def user_may_manage(record: dict[str, Any], user: dict[str, Any] | None) -> bool:
    """May this user edit, sign in / out, test or remove ``record``? A
    deck-wide server is the admins'; a private one its owner's; an ownerless
    private one (an OAuth server whose signer couldn't be found) the admins',
    so they can clean it up — they still can't call it."""
    if not _fd_auth_enabled():
        return True
    if record.get("deck_wide"):
        return _is_admin(user)
    owner = str(record.get("owner_id") or "")
    if owner:
        return str((user or {}).get("id") or "") == owner
    return _is_admin(user)


async def migrate_legacy_ownership(oauth_signer) -> list[str]:
    """Give every pre-ownership record an explicit owner / deck_wide.

    Non-OAuth records stay deck-wide (that is how they behaved). An OAuth
    record becomes private to whoever signed it in: ``oauth_signer(record)``
    returns that user's id, or "" when unknown — the record then stays
    private and ownerless (no agent can use it; an admin can remove it).
    Returns the names it changed.
    """
    path = _storage_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    changed: list[str] = []
    out: list[dict[str, Any]] = []
    for entry in raw:
        rec = _coerce_record(entry)
        if rec is None:
            continue
        if isinstance(entry, dict) and "owner_id" not in entry:
            if rec["auth"] == "oauth":
                rec["owner_id"] = str(await oauth_signer(rec) or "")
                rec["deck_wide"] = False
            changed.append(rec["name"])
        out.append(rec)
    if changed:
        await save_servers(out)
    return changed


async def upsert_server(record: dict[str, Any]) -> dict[str, Any]:
    """Insert-or-replace by ``name``. Returns the canonicalised record."""
    canonical = _coerce_record(record)
    if canonical is None:
        raise ValueError(
            "MCP server record requires 'name' plus either 'url' (http) or 'command' (stdio)"
        )
    if not canonical["added_at"]:
        canonical["added_at"] = time.time()

    existing = load_servers()
    replaced = False
    out: list[dict[str, Any]] = []
    for rec in existing:
        if rec["name"] == canonical["name"]:
            # Preserve previous added_at on update
            if rec.get("added_at"):
                canonical["added_at"] = rec["added_at"]
            out.append(canonical)
            replaced = True
        else:
            out.append(rec)
    if not replaced:
        out.append(canonical)
    await save_servers(out)
    return canonical


async def delete_server(name: str) -> bool:
    name = name.strip()
    if not name:
        return False
    existing = load_servers()
    kept = [rec for rec in existing if rec["name"] != name]
    if len(kept) == len(existing):
        return False
    await save_servers(kept)
    return True


# ── public-view helpers (used by routes) ────────────────────────────


def list_servers_public() -> list[dict[str, Any]]:
    """Return all configured servers with secrets masked."""
    return [_public_view(rec) for rec in load_servers()]


def get_server_public(name: str) -> dict[str, Any] | None:
    rec = get_server(name)
    if rec is None:
        return None
    return _public_view(rec)
