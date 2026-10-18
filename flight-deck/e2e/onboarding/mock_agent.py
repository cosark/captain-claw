"""Demo captain-claw agent for the kiosk guide's screenshots.

The same slice of the agent web API as ../hud/mock_agent.py (chat WebSocket,
files, datastore), with a believable conversation already in it, so a
screenshot shows the kiosk as someone would actually use it.

    python mock_agent.py PORT PERSONA      # PERSONA: research | inbox
"""

import json
import sys
import time

from aiohttp import WSMsgType, web

TOKEN = "mocktoken"

COMPARISON_MD = """# E-bike subscriptions in Germany

| Service | From / month | Minimum term | Included |
|---|---|---|---|
| Swapfiets Power 7 | €79 | 1 month | Repairs in 48 h, theft cover |
| Dance | €69 | 6 months | Servicing, insurance, app |
| Rebike | €59 | 12 months | Insurance, annual service |

## Recommendation

Price against Dance: same audience, monthly flexibility is our edge.
"""

HISTORY = {
    "research": [
        ("user", "Compare the three main e-bike subscription services in Germany for our pricing review: "
                 "price, minimum term and what's included. A table, then your recommendation."),
        ("assistant", """Here's how they compare (prices for a standard city e-bike, checked today):

| Service | From / month | Minimum term | Included |
|---|---|---|---|
| Swapfiets Power 7 | €79 | 1 month | Repairs in 48 h, theft cover |
| Dance | €69 | 6 months | Servicing, insurance, app |
| Rebike | €59 | 12 months | Insurance, annual service |

**Recommendation:** position against **Dance**. It targets the same city commuters, and a one-month \
minimum at **€72** would undercut its flexibility without starting a price war with Rebike's annual deal.

I saved the full comparison with sources to `saved/ebike-subscriptions.md` and the raw numbers to \
the *competitors* table."""),
    ],
    "inbox": [
        ("user", "Every morning, summarize what came in overnight and draft replies to anything urgent."),
        ("assistant", "Will do. I'll check your inbox at 8:00 and put the summary here, with drafts waiting in Gmail for you to send."),
    ],
}


def _check(request: web.Request) -> None:
    if request.query.get("token") != TOKEN:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")


def _file(logical: str, size: int, age: float, mime: str) -> dict:
    name = logical.rsplit("/", 1)[-1]
    return {"logical": logical, "physical": f"/srv/demo/workspace/{logical}", "filename": name,
            "extension": "." + name.rsplit(".", 1)[-1], "exists": True, "size": size,
            "modified": time.time() - age, "mime_type": mime, "is_text": mime.startswith("text/"),
            "source": "registry", "created_by": None}


def make_app(persona: str) -> web.Application:
    history = [{"role": r, "content": c, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")} for r, c in HISTORY[persona]]
    files_list = [
        _file("saved/ebike-subscriptions.md", len(COMPARISON_MD), 90, "text/markdown"),
        _file("saved/pricing-review-notes.md", 2400, 3600 * 26, "text/markdown"),
        _file("output/competitors.csv", 812, 120, "text/csv"),
    ] if persona == "research" else []

    async def files(request: web.Request) -> web.Response:
        _check(request)
        return web.json_response(files_list)

    async def file_view(request: web.Request) -> web.Response:
        _check(request)
        if request.query.get("path", "").endswith("ebike-subscriptions.md"):
            return web.Response(text=COMPARISON_MD, content_type="text/markdown")
        return web.json_response({"error": "File not found"}, status=404)

    columns = [{"name": n, "type": t, "position": i} for i, (n, t) in enumerate(
        [("service", "text"), ("monthly_eur", "integer"), ("min_term_months", "integer"), ("checked", "date")])]
    rows_data = [
        {"_id": 1, "service": "Swapfiets Power 7", "monthly_eur": 79, "min_term_months": 1, "checked": "2026-10-18"},
        {"_id": 2, "service": "Dance", "monthly_eur": 69, "min_term_months": 6, "checked": "2026-10-18"},
        {"_id": 3, "service": "Rebike", "monthly_eur": 59, "min_term_months": 12, "checked": "2026-10-18"},
    ]

    async def tables(request: web.Request) -> web.Response:
        _check(request)
        if persona != "research":
            return web.json_response([])
        return web.json_response([{"name": "competitors", "columns": columns, "row_count": len(rows_data),
                                   "created_at": "2026-10-18T09:00:00", "updated_at": "2026-10-18T09:12:00",
                                   "created_by": {"kind": "owner", "user_id": "", "name": ""}}])

    async def rows(request: web.Request) -> web.Response:
        _check(request)
        return web.json_response({"columns": ["_id"] + [c["name"] for c in columns], "rows": rows_data,
                                  "total": len(rows_data), "offset": 0, "limit": 100})

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        _check(request)
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        await ws.send_json({"type": "welcome", "session": {"id": "s1", "name": "default", "model": "demo",
                                                            "provider": "demo", "message_count": len(history)},
                            "models": [], "commands": [], "personalities": []})
        await ws.send_json({"type": "replay_batch", "messages": [
            {"type": "chat_message", "role": m["role"], "content": m["content"], "replay": True,
             "timestamp": m["ts"], "model": "demo"} for m in history]})
        await ws.send_json({"type": "replay_done"})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            if json.loads(msg.data).get("type") == "cancel":
                await ws.send_json({"type": "status", "status": "ready"})
        return ws

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/files", files)
    app.router.add_get("/api/files/view", file_view)
    app.router.add_get("/api/datastore/tables", tables)
    app.router.add_get("/api/datastore/tables/{name}/rows", rows)
    return app


if __name__ == "__main__":
    web.run_app(make_app(sys.argv[2]), host="127.0.0.1", port=int(sys.argv[1]), print=None)
