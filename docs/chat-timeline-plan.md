# Chat timeline: New topic dividers + Topics list (kiosk and full layout)

## Context

Kiosk users (and everyone else in Flight Deck's chat) have exactly one session control: "+ New" sends the agent's `/new`. It is a one-way door: the on-screen transcript is wiped, nothing lists or reopens an earlier conversation, and a typed `/session switch` moves the agent's context while the screen keeps the old transcript (the store never handles `session_switched` or `session_info`). Flight Deck also keeps a second log: `chat_messages` is keyed per agent lane (`chat_sessions.id` = lane key), not per agent session, and on open it *replaces* the agent's replay with that log. So after a reload the user sees every earlier session in one wall, before a reload only the current one.

Decision (2026-10-19): adopt the **messaging-app timeline** model. One continuous thread per agent lane, the way the WhatsApp/Telegram bridges already behave. "+ New" becomes **"New topic"**: it inserts a divider and resets the agent's context (still `/new`), but earlier messages stay visible. The Flight Deck log becomes the canonical transcript (it effectively is after a reload today). A small **Topics** list built from the dividers lets the user jump back to a topic or **resume** it (a `/session switch` under the hood, shown as a "Back on: …" divider). No second navigation axis, fits the phone header.

Decisions baked into this plan:
1. **Applies to both layouts** (store-level behaviour; the full layout gets the same dividers and Topics button). Keeping two models in one store would leave the full layout's reload wall as is.
2. **`/clear` becomes a divider too** ("Context cleared", agent context reset, screen not wiped). A wiped screen would come back on reload anyway.
3. **Titles, phase 1 = first user message of the topic, trimmed.** No LLM call. (Phase 2 can add agent-side short titles.)
4. **Shared agents excluded** (Flight Deck forwards no commands to them today; "+ New" is already hidden there). Topics button hidden for `shared`.
5. **Auth-off decks** keep an in-memory timeline only (no FD log there); after a reload only the agent's replay shows, as today.
6. Lanes need nothing special: each lane key already has its own FD log and its own agent session, so topics are per lane by construction.

## Agent side (tiny)

`captain_claw/web/slash_commands.py`
- `handle_command`: the final `command_result` send (line ~518) gains `"session": {"id", "name", "message_count"}` from `server._session_info()` (lane-aware via the lane view). Every command result carries the post-command session; the client uses it for `/new`, `/session new|switch`, `/clear`.
- No other change. `/new` already archives the outgoing session (`archive_current_session_to_history`) and broadcasts `session_info`; `/session switch` already refuses other members' sessions and lanes holding the session.

Test: new `tests/test_slash_command_session_payload.py` — fake server/ws as in `tests/test_speaker_sessions.py`; assert `/new` and `/session switch` results carry the new session id.

## Flight Deck backend

`captain_claw/flight_deck/db.py`
- `get_chat_messages(...)`: add `since_id: int | None` → rows with `id >= since_id` ascending (hard cap 2000), mutually exclusive with `before_id`.
- New `list_chat_topics(session_id, user_id) -> list[dict]`: select divider rows (`role='system' AND metadata LIKE '%"topic"%'` ordered by id) plus all `role='user'` rows (`id, content`) for the session; merge in Python → `[{row_id, agent_session_id, action, title, started_at, first_user_message, message_count}]` where `message_count` counts user+assistant rows between this divider and the next. Logs are small; two queries are fine.

`captain_claw/flight_deck/chat_routes.py`
- `GET /sessions/{id}/messages`: accept `since_id`.
- New `GET /sessions/{id}/topics` → `list_chat_topics(_store_id(...))`.

Test: new `tests/test_flight_deck/test_chat_routes.py` using the `deck` fixture pattern from `tests/test_flight_deck/test_profile_routes.py` (FlightDeckDB on tmp_path, `create_access_token`, httpx ASGI client): upsert session → add messages with two dividers → `/topics` returns two topics with first-user-message titles and counts; `since_id` returns the tail from the divider; another user gets 404/empty.

## Frontend store — `flight-deck/src/stores/chatStore.ts` (+ `services/agentChat.ts` for the type)

Types
- `ChatMessage` gains `rowId?: number` (FD row id) and `topic?: { action: 'new' | 'resume' | 'clear'; agentSessionId: string; title?: string; source: 'web' | 'agent' }`.
- `ChatSession` gains `agentSessionId: string`, `historyLoaded: boolean`, `hasOlder: boolean`.

Persistence
- `serverLoadMessages` keeps `rowId: r.id` and maps `meta.topic`.
- `_flushMessages` writes `topic` into metadata; after the POST, write the returned `ids` back onto the flushed messages' `rowId` (same order) so a fresh divider is jumpable.

Timeline rules (one helper, `markTopic(key, { id, name, messageCount, action?, source })`)
- Dedupe: no-op when the last divider in `messages` already has `agentSessionId === id`.
- Otherwise append a `system` message with `topic` (content = label text for old clients) and set `agentSessionId`.
- Callers:
  - `command_result` for `/new`, `/session new` → action `new`; `/session switch` → `resume`; `/clear` → `clear` (same session id, dedupe skipped for `clear`). Replaces today's `clearMessages(key)`; the "New session created…" system line is suppressed when a divider was inserted. `clearMessages` becomes unused → delete.
  - `session_info` (new handler): `message_count === 0 ? 'new' : 'resume'`, source `agent`. Covers a `/new` typed on another channel while the socket is open. (Broadcasts are already lane-scoped on the agent: main `_broadcast` skips non-main lanes, lane views send on their own socket.)
  - `welcome`: store `agentSessionId = data.session.id`; once `historyLoaded` is also true, `reconcileTopic(key)`: if the latest divider's id ≠ current id → `markTopic(..., source: 'agent')`. This retrofits existing logs on first open (one divider at the bottom) and catches sessions started while no browser was open. Auth-off: skip (no log).
- Rotation cue ("new topic" typed): already arrives as `command_result /new` then the user's text flagged `rotation_cue` → falls out of the above.

Canonical log + replay merge (fixes today's duplicate/loss behaviour)
- With the FD log loaded (`authEnabled`), a `replay_batch` is **merged**, not appended: for each replayed user/assistant message whose `(role, content)` is not among the log rows after the last divider, append it in order and persist it (the log heals, e.g. replies finished while no tab was open). Without a log (auth off) replay is used as today.
- `_openSession` on reconnect keeps the FD rows; the replay-strip stays for replay-flagged rows only.

New actions
- `resumeTopic(key, agentSessionId)`: no-op when busy or already current; sends `{type:'command', command:'/session switch <id>'}`.
- `loadOlder(key)`: `GET …/messages?before=<first rowId>&limit=500`, prepend, set `hasOlder = rows.length === 500`.
- `jumpToTopic(key, rowId)`: if a message with that `rowId` is loaded → return it (UI scrolls); else `GET …/messages?since_id=<rowId>&limit=2000`, replace the window, `hasOlder = true`, return the first message.
- A `suppressAutoScroll` ref/flag the UI honours for prepends and jumps (the auto-scroll effect is keyed on message count).

## Frontend UI — `flight-deck/src/components/agents/ChatPanel.tsx`

(Note: this file holds two literal NUL bytes in a template string; `grep` needs `-a`, `rg` works.)

- `LaneStrip` (line ~1086): "+ New" → "+ New topic" (wide: "New topic"), title "Start a new topic — the agent begins with a fresh context; earlier messages stay". Add a **Topics** button (`History` icon) beside it, hidden for `shared`. It opens `TopicsMenu` (new component in the same file or `components/agents/TopicsMenu.tsx`): fetches `/fd/chat/sessions/{key}/topics` on open (auth on; auth off → derive from loaded messages), newest first; each row: title (`title` or first user message trimmed to ~48 chars, fallback "Topic"), relative time, message count, "current" badge. Row tap = jump (scroll to the divider, `data-row-id` anchor); a per-row **Resume** button = `resumeTopic` (disabled when busy or current). Desktop: popover under the button; phone: full-width bottom sheet (same markup, positioned by `isMobile`).
- Divider rendering in the message bubble `role === 'system'` branch (line ~1839): when `message.topic` is set, render a centred rule with label "New topic" / "Back on: <title>" / "Context cleared" and the time; `id`/`data-row-id` for jumping. `isActivityMessage` already excludes non-narration system messages.
- Top of the list: an "Earlier messages" link when `session.hasOlder` → `loadOlder`.
- Kiosk guide copy in `flight-deck/src/components/layout/KioskOnboardingDialog.tsx` (line ~475): "+ New session" → "New topic", add one point for Topics ("jump back or resume an earlier topic").
- Light theme: write dark-first zinc classes (theme remap).

## Docs

- Copy this plan to `docs/chat-timeline-plan.md` at implementation start (repo convention for feature plans).

## Out of scope (later phases)

- Phase 2: agent-generated short titles when a session is archived (`archive_current_session_to_history` hook → `session.metadata.title` → `session_info.title`; FD updates the divider row), rename from the Topics list.
- Phase 3: shared agents (member-scoped session list; forward `/new` and `/session switch` for members, cap of 20 already enforced).
- HUD / Android app chat store (separate store, untouched).

## Verification

1. Backend tests, per file from a neutral dir with `PYTHONPATH` and the `os._exit` plugin (suite baseline rule): `tests/test_flight_deck/test_chat_routes.py`, `tests/test_slash_command_session_payload.py`, plus the existing `tests/test_speaker_sessions.py` and `tests/test_telegram_new_session.py` for regressions.
2. Frontend: `npm run build` in `flight-deck/` (tsc + vite); commit the rebuilt bundle under `captain_claw/flight_deck/static` with the source (FD build rule).
3. Live, on an isolated deck (never :25080/:25180): `FD_AUTH_ENABLED=true FD_DATA_DIR=<scratch>` on an alt port with one process agent, served from the built bundle. Walk, in the built-in browser:
   - send two messages → "+ New topic" → divider appears, earlier messages stay, "New session created" line absent;
   - send a message → reload → timeline intact with the divider, no duplicated rows;
   - Topics → two rows titled by their first user message; Resume the first → "Back on: …" divider, then ask "what did I say first?" to confirm the agent's context moved; Jump scrolls to the divider;
   - type `/session switch #2` → a resume divider via the command result; type `/clear` → "Context cleared" divider, screen kept;
   - phone preset (resize_window mobile): Topics opens as a sheet; kiosk-locked member sees the same.
4. Regression: a session that already has >500 rows shows "Earlier messages" and loads them without scrolling to the bottom.
