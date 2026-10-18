#!/usr/bin/env bash
# Regenerates the kiosk Getting started guide's screenshots
# (src/assets/onboarding/*-{dark,light}.webp) from a real kiosk: boots a
# throwaway Flight Deck (auth on) with a kiosk user, Google + Microsoft set up,
# a team Telegram bot and two demo agents, then shoots each screen in both
# themes. See README.md. Needs: uv, node, and Playwright with Chromium.
#
#   cd flight-deck && npm run build && cd .. && flight-deck/e2e/onboarding/run.sh
#   MODE=guide flight-deck/e2e/onboarding/run.sh   # shots of the guide itself
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
RUN="${E2E_RUN_DIR:-$HERE/.run}"
FD_PORT=${FD_PORT:-25191}
AGENT_PORT=${AGENT_PORT:-24191}
INBOX_PORT=$((AGENT_PORT + 1))
export E2E_RUN_DIR="$RUN"

PIDS=()
cleanup() { for p in "${PIDS[@]}"; do pkill -P "$p" 2>/dev/null || true; kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

rm -rf "$RUN"; mkdir -p "$RUN/fd-data"
cd "$REPO"
uv run python "$HERE/mock_agent.py" "$AGENT_PORT" research >"$RUN/research.log" 2>&1 & PIDS+=($!)
uv run python "$HERE/mock_agent.py" "$INBOX_PORT" inbox >"$RUN/inbox.log" 2>&1 & PIDS+=($!)
FD_DATA_DIR="$RUN/fd-data" FD_AUTH_ENABLED=true FD_JWT_SECRET=e2e-only-not-a-secret \
  uv run python -m captain_claw.flight_deck.server --host 127.0.0.1 --port "$FD_PORT" >"$RUN/fd.log" 2>&1 & PIDS+=($!)

FD="http://127.0.0.1:$FD_PORT"
for _ in $(seq 1 120); do
  curl -fsS "$FD/fd/auth/status" >/dev/null 2>&1 && break
  sleep 0.5
done

json() { python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(eval(sys.argv[2], {}, {"d": d}))' "$1" "$2"; }

# The first user on a fresh deck may register: the admin, who sets the deck up.
curl -fsS -X POST "$FD/fd/auth/register" -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"correct-horse-battery","display_name":"Admin"}' >/dev/null
ADMIN=$(json "$(curl -fsS -X POST "$FD/fd/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"correct-horse-battery"}')" 'd["access_token"]')
api() { curl -fsS -X "$1" "$FD$2" -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d "$3"; }

USER_ID=$(json "$(api POST /fd/admin/users '{"email":"maya@example.com","password":"correct-horse-battery",
  "display_name":"Maya Novak","role":"user","kiosk":true,"must_change_password":false}')" 'd["user"]["id"]')
# Kiosk mode for the users flagged for it (Admin → Settings → Kiosk mode).
api PUT /fd/admin/config '{"kiosk_mode":"selected"}' >/dev/null
# Placeholder OAuth clients: enough for "set up by your admin, press Connect".
api POST /fd/google/config '{"client_id":"demo.apps.googleusercontent.com","client_secret":"demo-secret"}' >/dev/null
api POST /fd/microsoft/config '{"client_id":"00000000-0000-0000-0000-000000000000","client_secret":"demo-secret","tenant":"common"}' >/dev/null
# The team bot without Telegram's check of the token (the admin route makes it):
# written straight to the deck's settings, never polled.
python3 - "$RUN/fd-data/flight-deck.db" <<'EOF'
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, datetime('now'))"
           " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
           ("fd:telegram-shared", json.dumps({"token": "123456:demo", "enabled": True, "auto_approve": True,
                                              "bot_username": "FlightDeckTeamBot", "bot_name": "Flight Deck"})))
db.commit()
EOF

# Maya's agents: two running demo agents and one powered off.
RESEARCH_PID=$(pgrep -f "mock_agent.py $AGENT_PORT research" | tail -1)
INBOX_PID=$(pgrep -f "mock_agent.py $INBOX_PORT inbox" | tail -1)
python3 - "$RUN/fd-data/.processes.json" "$USER_ID" "$AGENT_PORT" "$RESEARCH_PID" "$INBOX_PORT" "$INBOX_PID" <<'EOF'
import json, sys
path, owner = sys.argv[1], sys.argv[2]
rport, rpid, iport, ipid = (int(x) for x in sys.argv[3:7])
entry = lambda name, desc, port, pid, **kw: {"name": name, "description": desc, "web_port": port,
                                             "web_auth": "mocktoken", "pid": pid, "owner": owner,
                                             "provider": "anthropic", "model": "claude-sonnet-5-5", **kw}
json.dump({
    "research-assistant": entry("Research Assistant", "Market and competitor research", rport, rpid),
    "inbox-helper": entry("Inbox Helper", "Morning email summaries and reply drafts", iport, ipid),
    "report-writer": entry("Report Writer", "Monthly board reports", 0, None, stopped=True),
}, open(path, "w"), indent=2)
EOF

BASE="$FD" node "$HERE/shots.cjs"
if [ "${MODE:-assets}" = assets ]; then
  uv run python "$HERE/to_webp.py" "$RUN/png" "$HERE/../../src/assets/onboarding"
fi
