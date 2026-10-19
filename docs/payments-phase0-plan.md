# Payments, Phase 0: bill pay by barcode

Status: built on branch `feat/payments-phase0` (2026-10-19); off unless
`FD_PAYMENTS=1`. Not yet checked against real bank apps (§10).

## Goal

The agent does all the work of paying a bill. It reads the invoice, gets the
payee, IBAN, amount and reference right, checks them, and hands over a barcode
the user's bank app scans. The user finishes with one approval in their own
bank app.

Captain never moves money, never holds bank credentials and never sees card
numbers. Phase 0 needs no bank API, partner or license. Later phases add new
handoff routes to the same ledger: Revolut Business drafts (Phase 1), PSD2
payment links (Phase 2) and agent-checkout protocols (Phase 3).

## Ground rules

1. **Captain never pays in Phase 0.** No tool action moves money. The furthest
   the agent can take a payment is `handed_off`. Only the user sets `user_paid`.
2. **The model can't override a fraud warning.** Blocking warnings (IBAN
   changed, possible duplicate, over the cap, field not in the document) are
   cleared only by the signed-in owner in Flight Deck. No tool argument can
   clear one.
3. **No card data anywhere.** Card numbers, CVVs and expiry dates are removed
   before they are stored, shown to a model or echoed. The tool refuses
   card-like values in any field.
4. **Every field has a source.** Each field records where it came from:
   `barcode`, `document`, `model` or `user`. Fields that only the model read
   are marked for the user to check.
5. **No claim without the ledger.** The fabrication guard rejects any "paid"
   claim that the ledger doesn't back.
6. **Off by default.** `FD_PAYMENTS=1` turns it on for a deck.

## How it works

```
invoice: PDF · photo · Gmail attachment (get_attachment → Saved to:) · pasted text
  │  agent: payments prepare(source=…, fields the model read)
  ▼
agent tool (thin client) ── file bytes + fields ──▶ FD POST /fd/payments/agent/prepare
                                                     decode HUB3 / EPC barcode in the source
                                                     ground model fields in the document text
                                                     checks: IBAN, reference, payee history,
                                                             duplicates, cap
                                                     ledger row pay_ab12cd34ef56
                                                     status: draft | needs_review | ready
  ▼
agent: payments handoff(id) ──▶ FD renders HUB3 PDF417 and/or EPC QR, mints /p/<token>
  │                             (409 while a blocking warning is open)
  ▼ agent writes PNGs to saved/payments/<session>/; output carries Path: lines
FD chat: payment card · WhatsApp/Telegram: barcode + summary + /p/<token> link
  │
user scans in bank app → approves in the bank app (SCA) → taps "I paid"
  │                                     (card, /p page, or says so in chat)
  ▼
ledger: user_paid        (settled = bank-verified; reserved for Phase 1/2)
```

Flight Deck does all decoding, checking and rendering. The agent tool only
reads the file, calls Flight Deck and writes the PNGs it gets back. That keeps
the barcode libraries in one place, and agents running in containers need
nothing new. Flight Deck keeps the source file's SHA-256 and name, not the file.

## 1. `captain_claw/payments/`: pure package

No I/O except PNG rendering. Flight Deck imports it; the agent tool doesn't.

| Module | Does |
|---|---|
| `iban.py` | Normalizes an IBAN, checks mod-97 and per-country length (SEPA), and looks up the Croatian bank from the 7-digit bank code (VBDI) in `hr_banks.json`, built from HNB's register |
| `hr_ref.py` | Model `HRnn` + poziv na broj: format only (max 22 characters, digits and up to 3 dash-separated parts). `HR99` means no reference. Also the RF creditor reference (ISO 11649, mod-97) |
| `hub3.py` | Builds the HUB3 payload, renders the PDF417 PNG, parses a decoded HUB3 |
| `epc.py` | Builds the EPC payload and QR via `segno`, parses a decoded EPC |
| `decode.py` | Finds barcodes in image or PDF bytes with `zxing-cpp`; renders PDF pages with `pypdfium2` at 200 dpi |
| `grounding.py` | Checks that a field value appears in the document text: IBAN with or without spaces; amounts as `123,55`, `123.55`, `1.234,55` or `1,234.55`; references with or without dashes |
| `checks.py` | All warnings, as a pure function of the intent, the payee history, open intents and the cap |

### HUB3 (HUB spec v6, September 2022, EUR slips)

Fourteen fields, each followed by LF (0x0A), the last one included.
Fields are not padded except the amount. Field lengths count characters, not
bytes:

| # | Field | Max | Notes |
|---|---|---|---|
| 1 | Header | 8 | `HRVHUB30` |
| 2 | Currency | 3 | `EUR` |
| 3 | Amount | 15 | In cents, zero-padded: 123,55 → `000000000012355` |
| 4–6 | Payer name / street / postcode + city | 30 / 27 / 27 | Left empty in Phase 0, because the bank pays from the account the user is signed into (open question 2) |
| 7–9 | Payee name / street / postcode + city | 25 / 25 / 27 | |
| 10 | Payee IBAN | 21 | Croatian IBAN |
| 11 | Model | 4 | `HR01`, `HR00`, `HR99`, … |
| 12 | Poziv na broj | 22 | |
| 13 | Purpose code | 4 | ISO 20022, e.g. `COST`, `OTHR` |
| 14 | Description | 35 | |

- **Encoding:** UTF-8. Č, Ć, Đ, Š and Ž take 2 bytes but count as 1 character.
- **Allowed characters:** digits, the Croatian alphabet plus Q W X Y, space, and
  `, . : - + ? ' / ( )`. Anything else is transliterated (ä → a, ß → ss) or
  becomes a space. Text that is too long is cut. Every change raises a
  `text_adjusted` info warning that shows the before and after.
- **PDF417 settings:** 9 data columns, error correction level 4, binary (byte)
  compaction, module height:width 3:1, no Compact or Macro PDF417.
- **Prototype result:** `pdf417gen` 0.8.1 has no option to force byte
  compaction. Swapping `pdf417gen.compaction.get_optimal_compactor_fn` for
  `compact_bytes` during the call does work: the spec's own example (202 bytes)
  renders as 23 rows, and `zxing-cpp` 3.1.1 decodes it back byte-for-byte.
  - Keep the swap in one locked function and pin `pdf417gen` to `>=0.8,<0.9`.
  - A test checks that the data codewords start with the byte-compaction latch
    (901 or 924).

### EPC QR (EPC069-12 v3.0)

- **Payload:** `BCD` / `002` / `1` (UTF-8) / `SCT` / BIC (empty) / name ≤70 /
  IBAN / `EUR<amount>` (0.01–999999999.99) / purpose ≤4 / **either** a structured
  reference ≤35 (RF) **or** text ≤140 / info ≤70.
- **Limits:** at most 331 bytes, error correction level M, QR version 13 or
  lower. `segno.helpers.make_epc_qr` enforces these. The prototype payload came
  out at version 5 and decoded back exactly.

### Which barcode a payment gets

| Payee | Reference | Barcodes |
|---|---|---|
| Croatian IBAN | Croatian model (`HRnn` + poziv na broj) | **HUB3 only.** EPC has no model field, so the reference would only go as text and the payee couldn't match the payment |
| Croatian IBAN | RF reference, or none | HUB3 (main) + EPC |
| Other SEPA IBAN | any | EPC only |
| Not EUR | — | none; `non_eur` error |

## 2. Flight Deck ledger: `flight_deck/payments_store.py` → `payments.db`

Copy the `BatStore` pattern (`bat_store.py:164-190`): its own SQLite file in
`DATA_DIR` with WAL and a lazy DDL. Rows have `owner_id TEXT NOT NULL` with
**no** foreign key, the same as `public_file_shares` (`db.py:628-632`), because
an auth-off deck's owner is the synthetic `local` user. `db.delete_user`
(`db.py:1698`) also deletes the user's payment rows.

```sql
payment_intents(
  id TEXT PRIMARY KEY,              -- pay_<12 hex>
  owner_id TEXT NOT NULL, agent TEXT, session_id TEXT,
  status TEXT NOT NULL,
  payee_name TEXT, payee_street TEXT, payee_city TEXT, payee_oib TEXT,
  iban TEXT, bank_name TEXT,
  amount_cents INTEGER, currency TEXT NOT NULL DEFAULT 'EUR',
  model TEXT, reference TEXT, purpose_code TEXT, description TEXT, due_date TEXT,
  provenance TEXT,                  -- JSON {field: barcode|document|model|user}
  warnings TEXT,                    -- JSON [{code, severity, message, acked_by, acked_at}]
  source_name TEXT, source_sha256 TEXT, source_kind TEXT,
  rails TEXT,                       -- JSON ["hub3", "epc"]
  page_token_hash TEXT UNIQUE, page_expires_at REAL,
  supersedes TEXT,                  -- the intent a corrected prepare replaced
  automated INTEGER NOT NULL DEFAULT 0,
  created_at REAL, updated_at REAL, paid_reported_at REAL, paid_reported_via TEXT
);
payment_payees(owner_id, payee_key, iban, name, first_paid, last_paid, times,
               PRIMARY KEY (owner_id, payee_key, iban));
payment_events(id, intent_id, ts, actor, event, detail);   -- audit trail
```

| Status | Meaning | Set by |
|---|---|---|
| `draft` | A required field is missing (IBAN, amount, payee name) | prepare |
| `needs_review` | A blocking warning is open | prepare, checks |
| `ready` | Complete, no blocking warning open | prepare, owner ack |
| `handed_off` | Barcodes and page made | handoff |
| `user_paid` | The user says they paid | owner (card or `/p` page), or the agent's `mark` on a turn where the user said so |
| `skipped`, `cancelled` | Won't be paid, or a mistake / replaced | owner, or the agent's `mark` (human turns only) |
| `expired` | Untouched 30 days after the due date (or after creation if there is none) | sweep on list and prepare |
| `settled` | Bank-verified. **Never set in Phase 0** | Phase 1/2 |

- **Payee history.** `payee_key` is the OIB when known, otherwise the payee
  name normalized (case-folded, legal suffixes like `d.o.o.`, `d.d.`, `j.d.o.o.`
  and `obrt` removed).
  - Only `user_paid` (later `settled`) intents write to it.
  - A payment that was prepared but never paid doesn't count, so a fraudulent
    invoice prepared once never becomes "known".
- **Idempotency.** `prepare` returns the existing live intent (`existing: true`)
  for the same owner + `source_sha256`, or the same IBAN + amount + reference.
  This works like `find_live_spend`.
- **Corrections.** To fix a field the user tells the agent, which calls
  `prepare` again. The new intent sets `supersedes`, and the old one becomes
  `cancelled`. Phase 0 has no editing on the card.

## 3. Flight Deck routes: `flight_deck/payments_routes.py`

- **Wiring.** `configure(data_dir, …)` is mounted next to the public-share
  routers (`server.py:1507-1511`), before the SPA catch-all.
- **Flag.** `FD_PAYMENTS` accepts `1`, `true`, `yes` or `on`, is read on every
  call, and is off by default. When it is off, every payments route returns
  404.

**Agent routes.**
- **Identity:** `agent_identity.require_agent_caller`. The owner is
  `caller.owner`, or `local` when auth is off, the same as
  `public_file_shares._agent_caller`.
- **Shared-agent members are refused.** These paths are not added to
  `speaker_grants.GRANT_AWARE_PATHS`.

| Route | Does |
|---|---|
| `GET /fd/payments/agent/status` | `{enabled, deps_ok}`. Decides whether the tool is shown |
| `POST /fd/payments/agent/prepare` | `{source_b64?, source_name?, text?, fields?, session_id, automated, job_kind}` → intent. Source limit 10 MB; 30 prepares per hour per owner |
| `POST /fd/payments/agent/{id}/handoff` | → `{intent, page_url, images: [{kind, png_b64}]}`. 409 while `draft` or `needs_review` |
| `POST /fd/payments/agent/{id}/mark` | `{status, user_text, automated}`. 403 when automated. `user_paid` also needs `user_text` to say so (see §4) |
| `GET /fd/payments/agent/list`, `GET /fd/payments/agent/{id}` | `?status=&q=&due_within=` |

**Owner routes (JWT).**
- `GET /fd/payments`, `GET /fd/payments/{id}`, `GET /fd/payments/config`
- `GET /fd/payments/{id}/barcode/{hub3|epc}.png`
- `POST /fd/payments/{id}/ack` `{codes: [...]}`: the only way to clear a
  blocking warning. It moves `needs_review` to `ready`.
- `POST /fd/payments/{id}/mark` `{status: user_paid|skipped|cancelled}`

**Public page `/p/{token}`** (copied from `/s/{token}` in
`public_file_shares.py`).
- **The token:**
  - `secrets.token_urlsafe(24)`, stored as a SHA-256 like invite tokens, since
    it can change state.
  - It expires 14 days after handoff, or 1 day after a terminal status.
  - One `_gone()` page covers unknown, expired and revoked tokens.
- **The page:**
  - Payee, bank, IBAN, amount, model + reference, description and due date.
  - The barcodes as inline `data:` images.
  - Read-only fields with copy buttons. That needs one inline script, allowed
    by its CSP hash.
  - Two same-origin form POSTs, `/p/{token}/paid` and `/p/{token}/skip`
    (`form-action 'self'`).
- **Headers:** the same as `/s/` (no-store, no-referrer, noindex,
  `X-Frame-Options: DENY`). `BrowserGuardMiddleware` already refuses
  cross-site POSTs.
- **While a blocking warning is open:** no barcode and no buttons, just "Open
  Flight Deck to review" linking to `/payments/<id>`.
- **The link's base URL:** reuse `public_file_shares._agent_link_base`, moved
  into a shared helper.

**Notifications.** An intent prepared on an automated turn (mail triage, cron)
adds a bell notification:
`db.add_notification(owner, 'payment', …, ref_type='payment', ref_id=id)`.
A WhatsApp nudge via `fd_dispatch._nudge_waids` is optional and follows the
Bat ask pattern (`bat_routes.py:1016-1028`).

## 4. Agent tool: `captain_claw/tools/payments.py`

`PaymentsTool`, `name = "payments"`, actions `prepare`, `handoff`, `mark`,
`list` and `get`.
- **Parameters:** `action`, `source` (path), `text`, `payee_name`,
  `payee_street`, `payee_city`, `payee_oib`, `iban`, `amount` (a **string**
  such as `"123.55"`, never a float), `currency`, `model`, `reference`,
  `purpose_code`, `description`, `due_date`, `id`, `status`, `query`.
- **Never a parameter named `timeout`:** `registry.execute` reads that one
  (`registry.py:857`).

**Transport.**
- `fd_client.flight_deck_base()` + `flight_deck_headers()`, as in
  `public_link.py:106-125`. Refuse when `X-Agent-Auth` is missing.
- Members are refused the way `jev_client._member_call` does it.

**Visibility.**
- Register it in `_register_default_tools` (`agent_context_mixin.py:2443-2593`)
  with `metadata={"requires_payments": True}`, and add `"payments"` to
  `ToolsConfig.enabled` and `_ALWAYS_ENABLED` (`config.py:603-695`).
- `_resolve_tools` (`registry.py:452`) hides it unless
  `payments_client.payments_usable()` is true. That is a 120-second cache of
  `/agent/status` that fails closed, refreshed on owner turns like Jev
  (`agent_orchestration_mixin.py:2146-2159`).
- The member filter (`registry.py:471-482`) also drops `requires_payments`.

**Actions.**
- **`prepare`:**
  - Resolves `source` inside the agent's files, the way `public_link._resolve`
    does.
  - Refuses any field `card_scrub` flags.
  - Sends the bytes and fields to Flight Deck.
  - Prints a summary, the source of each field, the warnings and the next step.
- **`handoff`:**
  - Writes `saved/payments/<session>/pay-<id>-hub3.png` and `…-epc.png` with
    the `WriteTool` path helpers, the way `image_gen.py:76-95` does, and
    registers them in `_file_registry`.
  - Prints `Path:` lines, `Page: <url>` and a summary to repeat to the user.
- **`mark`:**
  - Refused when `mail_authority.current().mode == "automated"`.
  - The user's text comes from `kwargs["_agent"]._turn_user_text`, **not from
    a model argument**, so the model can't invent a "yes, I paid".
  - Flight Deck checks `user_paid` against a paid-intent pattern in English
    and Croatian: "paid", "I paid", "done, paid", "platio sam", "platila sam",
    "plaćeno", "uplatio", "uplaćeno".
- **Every result** includes a line `payment_intent: pay_… status: <status>`.
  The chat card keys on it, and the guard uses it as evidence.

**Delivery to the chat apps.**
- Add `"payments"` to `_DELIVERED_IMAGE_TOOLS` (`whatsapp_send_file.py:320`)
  and to the tool tuple in `collect_turn_generated_image_paths`
  (`platform_adapter.py:183`).
- WhatsApp and Telegram then send the barcode automatically. The reply adds
  the summary and the `/p` link.

**Prompt.**
- Add entries to `_TOOL_PROMPT_DESCRIPTIONS`, `_NANO` and `_MICRO`
  (`agent_context_mixin.py:89-174`).
- Add `instructions/section_payments.md`, included when payments are usable,
  the same way `section_jev.md` is (`agent_context_mixin.py:3341-3348`).
- What the section says:
  - Never ask for card numbers, CVVs or bank logins.
  - Never say you paid.
  - Prefer the barcode on the invoice to reading the numbers yourself.
  - Show every warning.
  - Ask the user to check any field marked "read by AI".
  - Give the barcode and the page link, and say they approve the payment in
    their bank app.

## 5. Chat card: `flight-deck/src/components/agents/PaymentCard.tsx`

**Why not the existing approval card.** Chat has no typed cards. The only
interactive one, `approval_request`, **approves itself after 15 or 60 seconds**
(`web_server.py:1145`), so payments must not use it.

**How the card is found instead.**
- The `monitor` event already gives the tool name and output
  (`chatStore.ts:1410`).
- Tool rows are saved with `tool_name` and `tool_output` metadata, so they come
  back after a reload.
- `groupActivity()` (`ChatPanel.tsx:2129`) splits out `payments` tool rows
  whose output has a `payment_intent:` line, and renders each as its own
  `<PaymentCard intentId>` instead of folding it into the activity group.
- The card always reads live state from `GET /fd/payments/{id}`. Yesterday's
  card shows "Paid ✓ 19.10." once the user taps "I paid" on their phone.

**What the card shows.**
- Payee and bank, the amount in large type, and the due date (red when
  overdue).
- IBAN, model + poziv na broj and description, each with a copy button and a
  source badge: *from barcode*, *from document*, *read by AI, please check*.
- Warnings:
  - Info warnings are grey.
  - Blocking warnings are amber, each with "I checked, continue" (`POST /ack`).
    The barcode appears only once all of them are cleared.
- The HUB3 barcode, with an EPC tab when there is one.
- "I paid" and "Skip". Afterwards a status chip replaces them.

**Elsewhere.**
- **Kiosk:** works automatically, since `SimpleLayout` mounts `ChatPanel`.
- **EmbeddedChat and the HUD:** show the plain tool text (Phase 0).
- **Pages:** `pages/PaymentsPage.tsx` for `/payments` (list, status filter) and
  `/payments/:id` (one card; the `/p` page links here for reviews).
- **New files:** `services/payments.ts` and `stores/paymentsStore.ts`.
- **Build:** `cd flight-deck && npm run build`, then commit the
  `captain_claw/flight_deck/static` bundle and check that `index.html` and
  `hud.html` point at assets that exist.

## 6. Card-number scrubber: `captain_claw/card_scrub.py`

This lives outside `payments/` because it protects every chat, not just
payments.

**What it finds.**
- **Card numbers.** All of these must hold:
  - 13–19 digits, either run together or grouped like a card (4-4-4-4,
    4-4-4-4-3, Amex 4-6-5, Diners 4-6-4) with single spaces or dashes.
  - The Luhn check passes.
  - The prefix is a real card range: Visa 4; Mastercard 51–55 and 2221–2720;
    Amex 34/37; Discover 6011, 644–649 and 65; Maestro 50 and 56–69; JCB
    3528–3589; Diners 300–305, 36 and 38–39; UnionPay 62.
  - It is not part of a longer alphanumeric token (IBAN, URL, hash).
  - It is not directly after `HRnn ` or "poziv na broj", so payment references
    survive.
- **CVV.** 3–4 digits after `CVV`, `CVC`, `CVV2`, `CVC2`, `CSC`, `CID`,
  `sigurnosni kod` or `sigurnosni broj`. Also a lone 3–4 digit number on the
  same line as a card number or expiry date.
- **Expiry.** `MM/YY` or `MM/YYYY` within 40 characters of a card number, or
  after `exp`, `expiry`, `valid thru`, `vrijedi do` or `istek`.

**What it writes instead:** `[card number removed · ends 4242]`,
`[CVV removed]`, `[expiry removed]`. Where user text was scrubbed, it adds:
`[Note: a payment card number was removed from this message. Captain never
needs card numbers or CVVs.]`

**Where it runs**, starting with the earliest place raw text appears:

| # | Where | Why there |
|---|---|---|
| 1 | Frontend, `chatStore.sendMessage` (`chatStore.ts:2001`), before sending and before the `/fd/chat` save. TS port in `flight-deck/src/lib/cardScrub.ts`, with a toast | Flight Deck saves the user's own message itself |
| 2 | `web/chat_handler.py handle_chat`, at entry (`:536`) | FD web, WhatsApp and device turns, before the echo, task naming and flows |
| 3 | `whatsapp_bridge._handle_message` (`:1717`) | Before the reaction-emoji LLM call (~`:2625`) and the bus broadcast (`:1623`) |
| 4 | `web/telegram.py _handle_telegram_message` (`:278`) | Before the `[TG user]` broadcast; Telegram skips `handle_chat` |
| 5 | `Agent.complete()` / `stream()`, next to `_absorb_surface_block` (`agent.py:619`) | Last line of defense for every other path (cron, flows, peers) |
| 6 | Outbound: assistant replies and tool results before `Session.add_message` | A model that read a card photo can't save or repeat the number |

**Tests.** One fixture, `tests/fixtures/card_scrub_cases.json`, holds:
- Must-catch: test card numbers from each network, in the different groupings.
- Must-keep: IBANs, poziv na broj, phone numbers, OIBs, order numbers, Luhn-valid
  numbers with a non-card prefix, dates.

Pytest runs the fixture. The frontend has no test runner, so a small node script
runs the same fixture against the TS port.

**Gap: photos of cards.** Phase 0 can't hide a card photo from a vision model:
- There is no local OCR. `image_ocr` uses an LLM, and tesseract, rapidocr and
  ocrmac are not installed.
- What Phase 0 does: row 6 keeps the number out of storage and replies, and the
  prompt tells the model not to transcribe it.
- Phase 1 option: a local OCR check at `chat_handler._all_images` (`:722`) that
  holds back images that look like cards.

## 7. Fabrication guard: new kind `false_payment`

Changes in `captain_claw/fabrication_guard.py`:
- `PAYMENT_CLAIM = "false_payment"` and `PAYMENT_TOOLS = frozenset({"payments"})`.
- **First-person claims are always false in Phase 0**, because no tool can pay:
  - English: "I paid", "I've paid", "I transferred", "I sent the money".
  - Croatian: "platio sam", "platila sam", "uplatio sam", "izvršio sam
    plaćanje", "poslao sam novac", "prebacio sam".
- **State claims are false unless there is evidence:**
  - English: "the invoice is paid", "payment completed / done / went through".
  - Croatian: "račun je plaćen", "plaćeno", "uplaćeno", "plaćanje je
    izvršeno/prošlo", "transakcija je izvršena".
  - Evidence is a `payments` result in this turn or the last 40 messages that
    shows `status: user_paid` (or later `settled`). The check mirrors
    `recent_mail`.
- **Not claims:**
  - Negations: "not paid", "unpaid", "nije plaćen", "neplaćen".
  - Questions.
  - Future or conditional: "once you pay", "after you've paid", "kad platiš",
    "nakon što platiš".
  - Instructions: "pay it in your bank app", "plati u aplikaciji".
  - "Paid" as an adjective: paid plan / tier / version / account /
    subscription / ads / leave, "plaćeni dopust".
  - Payments attributed to the user and backed by the ledger: "you marked it
    paid", "označio si kao plaćeno".
  - Third-party evidence: "HEP confirmed the payment", the same idea as the
    mail attribution filter (`:257`).
- **`TurnFacts`** gains `payment_tools` and `recent_payments`. The check runs
  only when payments were offered or used recently.
  `agent_orchestration_mixin._judge_reply` (`:887`) fills both in.
- **Corrective:** "Captain can't make payments, and nothing was paid in this
  turn. Say what is true: the payment is prepared (barcode ready) and the user
  approves it in their bank app. It counts as paid only when they confirm."
- **Backstop:** `PAYMENT_BACKSTOP = "Note: Captain did not make any payment. It
  can only prepare one for you to approve in your bank app."`
- **`_ID_SHAPES` (`:1014`)** gains `pay_[0-9a-f]{12}`, so the existing ID check
  catches made-up intent ids.
- **Held claims** (text next to tool calls) stay mail-only. Payment claims are
  judged on the final reply.
- **Tests:** about 25 cases in English and Croatian, added to
  `tests/test_fabrication_guard.py`.

## 8. Warnings

Severity:
- **error:** can't continue; the fields have to be fixed with a new `prepare`.
- **blocking:** the owner has to acknowledge it in Flight Deck.
- **info:** shown, nothing more.

| Code | Severity | When |
|---|---|---|
| `card_data` | error | A field looks like card data |
| `iban_invalid` | error | mod-97 or length check fails |
| `non_eur` | error | Currency isn't EUR (both Phase 0 barcodes are EUR-only) |
| `iban_changed` | blocking | This payee (OIB or name) was paid before to a **different** IBAN, the usual invoice-fraud pattern |
| `possible_duplicate` | blocking | Same source SHA-256, or same IBAN + amount + reference within 60 days, live or paid |
| `not_in_document` | blocking | A field the model gave isn't in the text of the document it came with |
| `barcode_mismatch` | blocking | A field the model gave differs from the decoded barcode. The barcode value wins and both are shown |
| `over_cap` | blocking | Amount is over the owner's cap (default €5,000) |
| `amount_unusual` | info | More than 3× the median of earlier payments to this payee |
| `new_payee` | info | Never paid this payee through Captain |
| `model_read_only` | info | IBAN, amount or reference came only from the model reading an image (no barcode, no document text). The card shows "please check" |
| `reference_format` | info | Poziv na broj doesn't fit the model's format, or the RF check fails |
| `overdue` | info | The due date has passed |
| `text_adjusted` | info | Characters were changed or cut for the barcode |

Fields are filled from the most reliable source first:
1. A barcode in the source: HUB3 or EPC.
2. Document text (pypdf), which confirms the fields the model gave.
3. The model alone, which gets a warning.

## 9. Build and deploy

- **New extra** in `pyproject.toml`:
  `payments = ["segno>=1.6,<2", "pdf417gen>=0.8,<0.9", "zxing-cpp>=3.1,<4", "pypdfium2>=4"]`.
  Only Flight Deck's environment needs it. `/agent/status` reports
  `deps_ok: false`, and the tool stays hidden, when it is missing.
- **Deploy:**
  1. Run `pip install .[payments]` on the deck host.
  2. Set `FD_PAYMENTS=1`.
  3. Restart Flight Deck.
  4. Restart the agents, so they register the tool.
  5. Build and commit the frontend bundle.
  6. Hard-refresh the browser.
- **Verify on a spare port**, not on the deck in use on :25080.

## 10. Tests

Run test files one at a time from a neutral directory (the whole suite hangs).

| Area | File | Covers |
|---|---|---|
| HUB3 | `tests/test_payments_hub3.py` | The spec example payload exactly; field cutting and character mapping; byte-compaction latch; render → zxing decode round trip; parsing |
| EPC | `tests/test_payments_epc.py` | The EPC spec examples; RF reference vs text; 331-byte limit; round trip |
| IBAN / reference | `tests/test_payments_iban.py` | mod-97 good and bad cases; lengths; Croatian bank lookup; HR model formats; RF check |
| Decode / grounding | `tests/test_payments_decode.py` | HUB3 and EPC found in PNG and in a generated PDF; amount and IBAN formats found in text |
| Checks | `tests/test_payments_checks.py` | Every warning code; that only paid intents become payee history |
| Store / routes | `tests/test_flight_deck/test_payments_routes.py` | State changes; idempotency; agent identity; member refusal; 404 with the flag off; 409 on handoff under review; acks only with the owner's JWT; `mark` refused when automated; `/p` headers, expiry and the gone page; rate limit |
| Tool | `tests/test_payments_tool.py` | Card-like field refused; user text taken from the turn, not the model; `Path:` lines; delivery whitelists |
| Scrubber | `tests/test_card_scrub.py` + fixture | Must-catch and must-keep; every call site in §6 |
| Guard | `tests/test_fabrication_guard.py` | The `false_payment` cases |

**Bank-app check by hand** (needed before turning it on for anyone):
- Generate a HUB3 code paying your own second account €0.01, then scan it with
  every Croatian bank app at hand.
  - Check that every field is filled in: IBAN, amount, model, reference,
    description, payee.
  - Check that an empty payer block is accepted.
  - Note which apps can scan from the photo gallery. That decides the advice
    for users on their phone.
- Scan the EPC code with a QR-capable app.
- Revolut can't scan PDF417 (an open community request), so Revolut users get
  copy-friendly fields.

## 11. Order of work

1. `payments/` package and `card_scrub.py` with their tests. No wiring yet.
2. Flight Deck store, routes, `/p` page and flag.
3. Agent tool, client, registration, prompt section, delivery whitelists and
   scrubber call sites. Needs 2.
4. Fabrication guard rule. Can run alongside 2 and 3.
5. Frontend card, payments page and bundle. Can start against the route
   contract in §3 alongside 3.

## Out of scope for Phase 0

- Moving money or calling any bank API. Revolut Business drafts are Phase 1;
  PSD2 payment links are Phase 2.
- `settled` status and matching payments against the bank feed.
- Verification of Payee name checks (needs a bank or aggregator).
- Non-EUR payments, card checkout and recurring payments.
- Editing fields on the card.
- Local OCR of card photos.
- Barcodes on the e-paper or StickS3 device: a possible stretch through
  `turn_media`.
- Per-model check-digit validation for poziv na broj (Phase 0 checks the
  format only).

## Decisions (agreed 2026-10-19)

1. **`/p/<token>` is a capability link:** works from WhatsApp without signing
   in, hashed token, 14-day expiry, no barcode while a review is open.
2. **HUB3 payer fields stay empty.** The bank pays from the signed-in account.
3. **The cap is per user:** €5,000 by default, changed on the Payments page.
4. **Mail triage and cron may prepare payments** for invoices they find; each
   one rings the bell.
5. **Payee history starts empty** and grows only from payments marked paid.

## As built (where it differs from the plan above)

- **HUB3:** every field ends with LF, the last one included (that's what the
  spec's example does).
- **No `payment_payees` table.** The history is the owner's paid intents, found
  by their `payee_key` column.
- **New error `not_sepa`** for an IBAN outside SEPA.
- **`barcode_mismatch` blocks only on money fields** (IBAN, amount, reference,
  model). A shortened payee name isn't a warning; the barcode's value is used.
- **No `section_payments.md`.** The model's rules live in the tool's
  description, so they're there exactly when the tool is.
- **Outbound scrub:** assistant replies and the results of the vision / OCR
  tools (`image_ocr`, `image_vision`, `video_vision`, `cv`) are scrubbed. Other
  tool results carry IDs the agent must use verbatim, so they're left alone.
- **The `/p` page sends `Referrer-Policy: same-origin`, not `no-referrer`.**
  With no-referrer the browser posts "I paid" with `Origin: null`, which
  `origin_guard` refuses as cross-site.
- **Barcodes are saved under `saved/media/<session>/`.** `payments/` isn't one
  of `WriteTool`'s categories.

## References

- HUB, *Uputa o upotrebi PDF417 2D bar koda … (obrazac HUB3A)*, v6, Sept 2022 —
  https://www.hub.hr/sites/default/files/inline-files/2DBK_EUR_Uputa_1.pdf
- EPC069-12 v3.0, *Quick Response Code: Guidelines to Enable the Data Capture
  for the Initiation of an SCT*, 13 Sept 2022 — europeanpaymentscouncil.eu
- segno EPC QR helper — https://segno.readthedocs.io/en/stable/epc-qrcodes.html
