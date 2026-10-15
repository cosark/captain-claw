# Captain Claw v0.8.0 Release Notes

**Release title:** Flight Deck for a Team · Agents in Your Inbox, Glasses and Pocket
**Release date:** 2026-10-15

0.8.0 is the biggest release since Flight Deck: three months, 75 merged pull
requests and nearly two hundred commits. It is about **putting agents in front
of other people.** Flight Deck becomes safe to run for a team — every route
knows who is calling, every Google and Microsoft account is per user, one agent
can be shared by several people, and a kiosk mode locks a shared machine down to
chat. Agents get real mailboxes — Outlook and Gmail, drafts, sends, attachments,
filing, a signature, and fully autonomous topic replies — and a set of guards
that stop them from repeating an email, writing one nobody asked for, or saying
they sent something they didn't. They reach you on more surfaces: WhatsApp files
both ways, `/new` on Telegram, a Flight Deck HUD on Meta Ray-Ban Display glasses
and a push-to-talk **M5Stack StickS3** in your pocket. And long work finishes:
**Bat**, a new orchestration mode that keeps going until an independent judge
says it's done, and a Vatra that no longer cuts its workers off mid-thought.

This is a minor-version bump for a reason: **some defaults change, mostly for
security.** Read [Upgrading](#upgrading) before you deploy — a deck reached
through its own domain must set `FD_ALLOWED_HOSTS` or `FD_PUBLIC_URL` first.

## Highlights

### Flight Deck for a team

The 0.7 decks were built by one person for one person. Before a second person
logs in, 0.8.0 closes what that assumption left open.

- **Every route knows who is calling.** Port-addressed agent proxies refuse
  anyone but the agent's owner. Code, Hosting, Basna, Vatra and peer
  consult/delegate routes take the owner from Flight Deck's own spawn records,
  never from a body `owner_id` — previously any client that reached the port
  could run shell as any account, or have Flight Deck read any file it could
  (its own database included). Scheduler jobs and flows have owners; stdio MCP
  servers are admin-only; provider keys never reach the browser.
- **A Host and Origin guard** on every HTTP and WebSocket route closes DNS
  rebinding and "any web page you visit can drive your local deck". CORS is no
  longer `*`.
- **Per-user Google and Microsoft.** Each teammate connects their own account;
  an agent only ever gets its **owner's** token, resolved from Flight Deck's
  records. Event pollers, Drive mounts and sends follow.
- **Registration closes** after the first admin. **Admin → Users** adds people,
  resets passwords, and administers any user's agents (create from an
  archetype, start, stop, configure, remove — each logged).
- **Team tier sets with team keys.** An admin publishes a tier set and teammates
  without models run on it; publishing shares the keys server-side, process
  agents missing a key get it and restart, and a spawn nothing can supply a key
  for is refused instead of producing a dead agent.
- **Shared agents** (`FD_AGENT_SHARING=1`): one agent, several people, each in
  private conversations. A member's turn uses the member's **own** Google, deep
  memory and files; members share the agent's datastore and `saved/` files as a
  commons with creators shown; anyone can publish a **context pack** (profile, a
  read-only folder, deep memory) to everyone on the agent; and the owner's agent
  gets a `shared_agent_usage` tool, walled off from outbound actions after it
  reads member text.
- **An owner profile in every prompt.** Each user's About me, Company and
  Preferences — plus deck-wide defaults from the admin — reach every agent they
  own on every turn, with no restart. (Eco agents also start obeying their
  per-agent instructions, which a template bug had silently dropped.)
- **The simple layout and kiosk mode.** A chat-first layout — agents with a busy
  pill and power switch on the left, chat in the middle, the agent's files and
  datastore on the right (Cmd/Ctrl+\\). `--simple-chat` locks non-admins into it
  on a shared machine, with an archetype picker for new agents, their own
  Connections in a dialog, and a clean sign-out.
- **Connections, one tab per service** — Google, Microsoft, ChatGPT,
  Antigravity, MCP Servers, WhatsApp, Deep memory, Jev — with status dots and
  state that survives a tab switch.

### Agents in your inbox

- **Microsoft 365.** `microsoft_mail`, `microsoft_calendar` and
  `microsoft_onedrive` are the Outlook twins of the Google tools — the same
  per-user connection through Flight Deck, owner-only. Outlook mail reads,
  searches, drafts (replies inside the conversation) and files; it **never
  sends**. Calendar changes that would make Outlook email attendees need
  `notify_attendees: true` and a turn where a person asked. OneDrive reads
  Office and PDF inline, uploads large files in sessions, and never overwrites.
- **Gmail sends — opt-in, audited.** `google_mail` gains `send`, `send_draft`,
  `update_draft` and `list_drafts`. Under Flight Deck every send goes through
  one gate with a per-user policy (off by default), recipient allowlist, daily
  limit, duplicate suppression, an audit table and a bell per send. The old
  `gws` CLI wrapper, which could reach Gmail around that gate, is **retired**.
- **Never the same email twice.** Drafts and sends check Drafts and recent Sent
  for the same recipient and subject and refuse a repeat unless you ask for
  another copy — in Gmail and Outlook.
- **Attachments both ways.** `get_attachment` opens what arrives (each email's
  files in their own folder, the right reader named for each); drafts and sends
  attach `saved/` and `vfs:` files (20 files / 25 MB, secrets and databases
  refused).
- **Filing.** Archive, move, labels and folders (Gmail), categories (Outlook) —
  up to 100 conversations per call, with an undo hint, and never a trash or a
  delete. Organizing is its own permission.
- **Google Sheets and Docs edited in place** — `sheet_read/update/append/clear`
  and `doc_read/replace_text/append_text/insert_text` on the same file id,
  instead of replacing or re-uploading the whole file.
- **An agentic signature** you set per account, added to every email and draft
  your agents write.
- **Topic auto-replies** (Gmail, off by default): an agent answers inbound mail
  that is *only* about one of your narrow topics, with no confirmation. Topics
  are narrow by construction (lint, a breadth test on your real mail, a drift
  breaker), senders must pass Gmail's own authentication, an outbound guard
  blocks secrets, card and bank numbers, internal hosts and your never-disclose
  terms, a second judge reads every reply, and anything doubtful waits in
  Flight Deck as a held card. `FD_GMAIL_AUTOREPLY=off` stops it deck-wide.

### Agents that don't make things up

- **No email nobody asked for.** Autonomous Work had drafted ~40 unrequested
  "Re: …" replies once it had earned trust. Mail actions are now human-only —
  proposed, never auto-fired by grant or trust — and every automated turn
  (scheduler, flows, plans, peers, workers, MCP tasks) may write mail only when
  its own human-written text asks for that email.
- **A fabrication guard.** A DeepSeek agent answered six send requests in a row
  with "**Sent** …", invented Message IDs and narrated searches — with zero tool
  calls. Every final reply is now judged: a claimed send or calendar change in a
  turn where none happened, an ID nothing ever showed the model, or a narrated
  lookup with no tool call is rejected (never replayed) and the agent is asked —
  at most twice — for a **true** reply. The corrective never tells it to go and
  make the send.
- **A garbled reply gets a way out.** With a Jev key connected, a reply that
  came out as a loop, token soup or raw tool-call JSON gets a notice
  recommending `/new` on the web, WhatsApp, Telegram and the HUD.

### Channels and devices

- **WhatsApp, both ways.** Agents send pictures, video, audio and documents back
  (type decided from the file's bytes, falling back to a document that always
  arrives) and deliver what a turn made once the reply is out. Every file you
  send reaches the agent — documents, albums, stickers, voice notes (transcribed,
  recording kept) — and a message sent while the agent is busy is retried in
  order instead of lost. Automated results arrive **once**, formatted. Messages
  get an emoji **reaction**. `/new` now answers.
- **Telegram `/new`** starts a fresh session for that user (the old one is kept
  in history).
- **Only deliverables are auto-sent.** Chat bridges used to post any file named
  on a `Path:` line in tool output — a forged line in an email or web page could
  post any host file into the chat. Now a file goes only if this turn wrote it
  inside the agent's `saved/` area.
- **A Flight Deck HUD for Meta Ray-Ban Display** at `/hud/`: chat with your
  agents (approvals, Stop, read-aloud), read their Markdown files, browse the
  datastore — about 150 KB on the wire, paired with a code you approve at
  `/hud/pair`.
- **The StickS3 voice client.** Hold the button, talk, release: Flight Deck
  transcribes, one of your agents answers, and the M5Stack StickS3 shows the
  reply, reads it aloud and shows any picture — with the agent's live activity
  ("Searching: …", "12s · 3 steps") while it works. Firmware in
  `firmware/sticks3/`, server at `/device/ws`.
- **Claude can drive your fleet.** Flight Deck is now an MCP server at
  `/fd/mcp-server`: claude.ai and Claude Desktop connect with OAuth 2.1, Claude
  Code with a personal access token, and get `list_agents`, `send_task`,
  `get_result`, `cancel_task` (and `jev_ask`) over your own agents.

### Long work that finishes

- **Bat — the stubborn finisher.** A new orchestration mode for work that must
  get done: it plans, runs checkpointed steps with retries, survives Flight Deck
  restarts, and stops only when an independent, fail-closed judge says the task
  is complete. A hard safety floor keeps its workers from catastrophic commands;
  a start gate asks you before a plan that sends email, spends money or creates
  accounts; 2FA codes and passwords are always asked of you. A Bat page shows
  steps, live agents, progress, result and files.
- **Vatra workers run until they're done** — no ~30-minute cut-off, a full
  reading toolkit for every worker, 30-minute team planning, the Lead on the
  tier's full output limit (up to 64k), and lifted web-fetch caps.
- **An in-turn context governor.** A Vatra worker does its job in one long
  turn, and on a 100k tier its input had ballooned to ~220k, ~17M uncached input
  tokens in one run. It is now budgeted in real tokens, cut once and held so the
  cache stays valid, with big results spilled to files, older ones folded, and
  fetched pages de-duplicated run-wide. Workers read a rule-built **run map**.
- **Long-form deliverables on weak models.** A Captain Spark story run shipped
  an 88-byte file reading `[written to disk: ...]`. Runs now declare their
  deliverable, assemble it by code, and can't reach `done` on a missing,
  placeholder or gapped one; the `write` tool refuses empty or placeholder
  content and reads every write back. Plus the opt-in **Story Integrity
  Protocol** (deterministic checks that can block, model passes that only
  revise, advisory scores) and a per-tier **Weak / fast model** checkbox.
- **Runs always end** — an exception leaves `error` (continuable), never a run
  stuck on `running`.
- **Code** vetoes a "clean" verdict while an armed test gate is red, fails
  closed on unparseable triage, and ends every session with a committed
  `HANDOFF.md`.
- **Basna** keeps the judge's label beside your thumbs vote and measures the
  judge against you — Cohen's kappa, a confusion matrix, exports.
- **Six opt-in Loops & Graphs levers** — parallel-first planning, flow
  reference lint, a deterministic council tally, pushed dependencies,
  blast-radius gating and constraint learning.

### Context that holds up

- **A crowded-session context engine.** Weak models degraded as sessions grew:
  ~15 notes were appended after the question as assistant turns, and a
  per-minute clock in front of the history broke the prompt cache. Background
  notes now form one frozen block per turn, every message carries its origin,
  correctives and rejected replies are never replayed, and a cut-off answer is
  continued. On a production replay a turn's first request now repeats 96–98% of
  the previous one (5.6% was cached before).
- **Automated turns get their own lane** (`AUTO`), so a scheduler job no longer
  occupies the main chat; "new topic" / "nova tema" starts a fresh session;
  compaction writes a deterministic topic digest instead of an LLM summary.
- **Topics** are built from the conversation only, ranked, pinnable, and can
  recall one earlier topic into a turn (shadow mode by default).
- **Deep memory actually searches by meaning.** Its vector width defaulted to
  1536 while the default embedder emits 256, so every vector was silently
  dropped and "folk legends from the Balkans" found nothing in a folk-tale
  corpus. The width is auto-detected, scores are 0..1, and deep memory is now a
  Flight Deck capability: one admin connection, agents hold no key, every
  search scoped to the owner.
- **Google Drive folders as VFS projects.** Mount a Drive or Shared Drive
  folder read-only; agents read, extract and OCR its files as if they were
  local, fetched on demand — or clone it to real Markdown for grep and deep
  memory.

### Jev

[Jev](https://docs.typesafe.ai) (TypeSafe AI) gives fast, calibrated judgments
about text — yes/no, choice and rubric questions with probabilities. Each user
connects their own key; agents get a `jev` tool that calls through Flight Deck
with the owner's key, flows get a `tool: jev` step, the inbound MCP server gets
`jev_ask`, and Connections has a playground. While a key is usable, agents are
told to **ask Jev first** whenever a task means judging content.

### Models and providers

- **Thinking models stop 400-ing**: `tool_choice` and temperature-pinned
  models (kimi-k3) self-heal, a JSON answer hidden in `reasoning_content` is
  recovered, inline `<think>` is preserved, and DeepSeek V4's orphan tool
  results no longer break follow-ups.
- **OpenAI gpt-6** gets `max_completion_tokens`, and tiers can set a
  **reasoning effort** (OpenAI, DeepSeek; `none` included).
- **Local servers**: an output budget the server's window can't hold shrinks
  itself instead of failing every turn with an empty reply; a stream that dies
  before any text reports the server's real error.
- **Google subscription text** through the Antigravity CLI (contributed), and
  **You.com** as a web-search provider (contributed). An experimental text-only
  provider for the `claude` CLI is in the tree but not wired into Flight Deck.
- **Agents start fast offline**: the cached embedding model no longer asks the
  Hugging Face Hub on every start (75 s → 0.1 s with the Hub unreachable).

### Iskra

A **Reports** view has a Deep-Researcher narrate how the village worked over
any period — Quick or Deep, numbers straight from the ledger, saved and
readable in a proper reader. Beings stop pacing the village all day: one walk
per heartbeat, then rest until the mind has witnessed the arrival.

### Lupa — a research desk on Flight Deck

`lupa/` ships **Lupa**, a standalone B2B research desk built purely on Flight
Deck's HTTP API: streams of research with a plan gate, reports with a
**receipts** panel (verdict, confirmed and refuted claims, facts ledger, cost
with ROI), deepening rounds, standing briefs and a Basna second opinion — and
**Kalup**, a Pack Studio that turns a desk description into a published
vertical desk behind a green ship-gate. See `lupa/README.md`.

### Also

- **The Task Planner distributes** a reviewed plan round-robin across lanes.
- **Chat stays fast** on older machines: no more whole-app re-renders on every
  WebSocket event.
- **The archetype grid** (`FD_ARCHETYPE_GRID=1`, off by default) composes
  `reviewer.legal`-style archetypes from a function and a domain, with
  domain-sliced deep-memory recall.
- **Each user binds their own WhatsApp number** for Autonomous Work nudges in
  Connections (allowlisted, test send included).
- The bundle directory lost 268 orphaned assets (~400 MB).

## Upgrading

**Before you deploy**

1. **A deck reached through its own domain** (a reverse proxy, a tunnel) must
   set `FD_ALLOWED_HOSTS=<domain>` or `FD_PUBLIC_URL=https://<domain>` on Flight
   Deck, or every request — the SPA, WhatsApp/Meta webhooks, OAuth callbacks,
   inbound MCP, the StickS3 — is refused. LAN access by IP or machine name needs
   nothing. `FD_CORS_ORIGINS` no longer defaults to `*`.
2. **Set a fixed `FD_JWT_SECRET`** on any shared deck (inbound MCP OAuth and Lupa
   need it stable). The full team checklist is in
   `docs/team-deployment.md`.
3. **Pull, restart Flight Deck, then restart every agent, then hard-refresh.**
   The order matters: until an agent restarts, its peer and token calls get 403.
   **Docker agents need an image rebuilt on 0.8.0** and `FD_URL` +
   `FD_AGENT_SHARED_SECRET` in their env.

**After you deploy**

- **Reconnect Google once per user.** Google is per user now; the primary owner
  keeps working on the old deck-wide token until they reconnect.
- **Publish your team tier set again** if you published one before.
- **Enable the Google Sheets API and Google Docs API** in the deck's Google
  Cloud project for in-place editing; tick **Gmail (read + modify)** in
  Connections → Google (users reconnect) for mailbox filing; add `gmail.send`
  (or compose) for topic auto-replies.
- **Microsoft 365:** register an app in Entra ID with the Web redirect URI
  `https://<deck>/fd/microsoft/callback`, then fill **Connections → Microsoft**.
- **Several decks on one host:** a deck isolated with `FD_DATA_DIR` that should
  keep its MCP servers copies `~/.captain-claw-fd/mcp_servers.json` into its
  data dir once.
- **Review cron jobs** the agent start log flags with `legacy cron job may write
  email`.
- **WhatsApp nudges** (with auth on) need each user's number in Connections →
  WhatsApp.

**Removed and changed**

- `gws` (Google Workspace CLI) is retired — stripped from configs and
  archetypes on load; use `google_drive`, `google_calendar`, `google_mail`.
  `/api/gws-status` is now `/api/gdrive-status`. Auth-off decks get no Google
  through Flight Deck.
- The Cmd/Ctrl+1–5 page-jump shortcuts are gone.
- Self-registration is closed after the first account (`FD_REGISTRATION_OPEN=1`
  reopens it); existing scheduler jobs and flows become admin-owned and keep
  firing; stdio MCP servers are admin-only.
- Chat bridges no longer auto-send Drive downloads or email attachments.
- Autonomous Work never auto-fires a mail action.
- `web_fetch` defaults: 200k chars per page (was 100k), 100k per URL and 400k
  total in a batch (was 25k / 150k).
- New defaults that change what an agent sees: `insights.context_mode:
  relevant`, `context.compaction_summary: digest`, `session.automation_lane:
  AUTO`. Set `importance`, `model` or `""` for the old behaviour.
- The `write` tool refuses empty or placeholder content and reads writes back
  (`CLAW_WRITE_GUARD=0` turns it off).
- `deep_memory.embedding_dims` defaults to `0` (auto-detect).

**New settings**

| Setting | Default | What it does |
|---|---|---|
| `FD_ALLOWED_HOSTS` | — | Extra host names the deck answers to |
| `FD_REGISTRATION_OPEN` / `_DISABLED` | — | Reopen / force-close self-signup |
| `FD_SIMPLE_CHAT` / `--simple-chat` | off | Kiosk: non-admins get only the simple layout |
| `CAPTAIN_CLAW_FD_HOME` | — | Per-instance home for MCP servers, apps, agent secret |
| `FD_AGENT_SHARING` | off | Allow sharing an agent with other users |
| `FD_ARCHETYPE_GRID` | off | Function × domain archetypes |
| `FD_BAT_DISABLED` / `FD_BAT_SPEND` | Bat on / spend off | Bat kill switch / allow real-money spend |
| `FD_GMAIL_SEND` / `FD_GMAIL_AUTOREPLY` | — | `off` = deck kill switch for agent sends / auto-replies |
| `WHATSAPP_REACTIONS` | on | `off` disables emoji reactions |
| `tools.jev.check_replies` | `true` | Garbled-reply notice suggesting `/new` |
| `tools.google_mail.allow_send` | `false` | Standalone agents may send Gmail |
| `tools.google_mail.repeat_check_days` / `tools.microsoft_mail.repeat_check_days` | `14` | Sent window for the repeat check (0 = off) |
| `tools.web_search.provider` | `brave` | Now also `youcom` (`YDC_API_KEY`) |
| `whatsapp.auto_send_media` / `max_media_per_turn` | `true` / `6` | End-of-turn file delivery to WhatsApp |
| `context.compaction_summary` | `digest` | `model` = the old LLM summary |
| `context.notes_allocator` | `trim` | `capped` = cut each note to its share |
| `context.turn_governor` | `false` | In-turn context governor (Vatra turns it on for workers) |
| `session.automation_lane` | `AUTO` | `""` keeps automated turns in the main chat |
| `session.rotation_cues` | "new topic", "nova tema", … | Phrases that start a fresh session |
| `conversation_topics.recall` | `shadow` | `on` sends the topic recall card |
| `deep_memory.embedding_dims` / `min_score` | `0` / `0.12` | Auto-detected width / relevance floor |

New tables and databases (`bat.db`, `notifications`, `context_packs`,
`personal_access_tokens`, OAuth tables, `gmail_sends`, the auto-reply ledger,
`being_reports`, `vatra_ledger`, …) create themselves on restart. No manual
migration.

**New pieces to install (optional)**

- **StickS3:** `uv tool install platformio`, then `cd firmware/sticks3 && pio
  run -t upload` (`firmware/sticks3/README.md`).
  The deck needs `SONIOX_API_KEY`; agent-made audio needs `ffmpeg`.
- **Lupa:** `pip install -e lupa/api`, sharing `FD_JWT_SECRET` with Flight Deck.
- **Antigravity:** the Antigravity CLI installed and signed in on the host.
