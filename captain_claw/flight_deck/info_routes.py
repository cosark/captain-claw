"""The signed-in user's corner of the deck, in one read — the kiosk Info dialog.

``GET /fd/info`` returns:

* ``tiers``       — the tier set their agents are created on (their own Library
  set, else the admin's team default, as an archetype spawn resolves it): per
  tier the provider, model, endpoint host and context sizes, and how many
  archetypes use it. Never a key.
* ``archetypes``  — how many they can create agents from, by source.
* ``profile``     — the deck-wide defaults, their own profile, and the exact
  text their agents receive (full, and compact for small-model agents).
* ``account``     — plan and limits, role, kiosk lock.
* ``connections`` — which of Google, Microsoft, Telegram and Jev are set up.

Their agents the SPA already lists (``/fd/processes``, ``/fd/containers``), so
they are not repeated here. Each part is best-effort: one that can't be read
comes back empty rather than failing the whole read.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends

from captain_claw.flight_deck import tenant_profile as tp
from captain_claw.flight_deck.auth import get_current_user, get_db
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["info"])


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


async def _tiers(db, uid: str, archetypes: list[dict]) -> dict[str, Any]:
    from captain_claw.flight_deck.admin_routes import _endpoint_host
    from captain_claw.flight_deck.basna_routes import _load_registry, owner_tier_set

    resolved = await owner_tier_set(db, uid)
    try:
        registry = _load_registry().get("tiers") or {}
    except Exception:
        registry = {}
    own = resolved["tiers"] if isinstance(resolved["tiers"], dict) else {}
    used = Counter(str(a.get("tier") or "") for a in archetypes)
    rows: list[dict[str, Any]] = []
    for name in [*registry, *(t for t in own if t not in registry)]:
        tier = own.get(name) if isinstance(own.get(name), dict) else {}
        default = registry.get(name) if isinstance(registry.get(name), dict) else {}
        # A tier the set leaves empty falls back to the deck's built-in one.
        src = tier if tier.get("model") else default
        base_url = str(src.get("base_url") or "").strip()
        rows.append({
            "id": name,
            "label": str(default.get("label") or name),
            "use": str(default.get("use") or ""),
            "provider": str(src.get("provider") or ""),
            "model": str(src.get("model") or ""),
            "endpoint": _endpoint_host(base_url) if base_url else "",
            "input_ctx": _int(src.get("input_ctx")),
            "output_ctx": _int(src.get("output_ctx")),
            "vision": bool(tier.get("vision")) if tier.get("model") else False,
            "from_set": bool(tier.get("model")),
            "archetypes": used.get(name, 0),
        })
    return {"source": resolved["source"], "set_name": resolved["name"], "tiers": rows}


async def _connections(db, uid: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        from captain_claw.flight_deck import google_oauth_routes as g

        tokens = await g._load_tokens(db, uid)
        out["google"] = {"configured": await g._effective_oauth(db) is not None,
                         "connected": bool(tokens and tokens.refresh_token)}
    except Exception as exc:
        log.debug("info: google status unavailable", error=type(exc).__name__)
    try:
        from captain_claw.flight_deck import microsoft_oauth_routes as m

        tokens = await m._load_tokens(db, uid)
        out["microsoft"] = {"configured": await m._token_client(db) is not None,
                            "connected": bool(tokens and tokens.refresh_token)}
    except Exception as exc:
        log.debug("info: microsoft status unavailable", error=type(exc).__name__)
    try:
        from captain_claw.flight_deck import telegram_shared as ts

        cfg = await ts.load_config(db)
        link = await db.get_telegram_link(uid)
        out["telegram"] = {
            "configured": bool(cfg["token"] and cfg["enabled"]),
            "status": (link or {}).get("status") or "",
            "linked": bool((link or {}).get("tg_user_id")),
            "agent": str((link or {}).get("agent_name") or (link or {}).get("agent_slug") or ""),
        }
    except Exception as exc:
        log.debug("info: telegram status unavailable", error=type(exc).__name__)
    try:
        from captain_claw.flight_deck.jev_routes import status_for_owner

        jev = await status_for_owner(uid)
        out["jev"] = {"connected": bool(jev.get("key_set")), "source": jev.get("source") or ""}
    except Exception as exc:
        log.debug("info: jev status unavailable", error=type(exc).__name__)
    try:
        from captain_claw.flight_deck.soniox_routes import status_for_owner as soniox_status

        soniox = await soniox_status(uid)
        out["soniox"] = {"connected": bool(soniox.get("key_set")), "source": soniox.get("source") or ""}
    except Exception as exc:
        log.debug("info: soniox status unavailable", error=type(exc).__name__)
    return out


@router.get("/fd/info")
async def my_info(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    from captain_claw.flight_deck.admin_routes import user_kiosk_locked
    from captain_claw.flight_deck.archetypes import merged_archetypes
    from captain_claw.flight_deck.rate_limiter import get_user_limits, get_user_plan

    db = get_db()
    uid = str(user["id"])
    out: dict[str, Any] = {}

    archetypes: list[dict] = []
    try:
        archetypes = await merged_archetypes(db, uid)
    except Exception as exc:
        log.warning("info: archetypes unavailable", error=type(exc).__name__)
    by_source = Counter(str(a.get("source") or "base") for a in archetypes)
    out["archetypes"] = {"total": len(archetypes), "base": by_source.get("base", 0),
                         "own": by_source.get("user", 0), "shared": by_source.get("shared", 0)}

    try:
        out["tiers"] = await _tiers(db, uid, archetypes)
    except Exception as exc:
        log.warning("info: tiers unavailable", error=type(exc).__name__)
        out["tiers"] = {"source": "none", "set_name": "", "tiers": []}

    try:
        full, compact = await tp.compose_for_owner(db, uid)
        out["profile"] = {"own": await tp.load_profile(db, uid), "deck": await tp.load_deck(db),
                          "preview": {"full": full, "compact": compact}}
    except Exception as exc:
        log.warning("info: profile unavailable", error=type(exc).__name__)
        out["profile"] = None

    row = (await db.get_user_by_id(uid)) if db is not None else None
    row = row or user
    out["account"] = {
        "email": str(row.get("email") or ""),
        "display_name": str(row.get("display_name") or ""),
        "role": str(row.get("role") or "user"),
        "created_at": str(row.get("created_at") or ""),
        "plan": get_user_plan(row),
        "limits": get_user_limits(row),
        "kiosk": await user_kiosk_locked(db, row) if db is not None else False,
    }
    try:
        from captain_claw.flight_deck import usage_quota

        out["account"]["usage"] = await usage_quota.status(db, row)
    except Exception as exc:
        log.warning("info: usage unavailable", error=type(exc).__name__)
    out["connections"] = await _connections(db, uid)
    return out
