# Payments, Phase 4: the bank statement closes the loop

Status: **plan agreed** (2026-10-19): all six decisions at the end were taken
as recommended. **4a and 4b are built** (see "As built" below); 4c and 4d
are next.
It builds on Phases 0–2: the same ledger, the same Enable Banking application
as pay-by-bank links, and the same Revolut Business connection.

## As built: 4a (2026-10-19)

Tested against a fake Enable Banking AIS and a fake Revolut. Not yet run
against a real bank account. On a spare deck, the real Enable Banking API
answered the AIS calls (`/aspsps?service=AIS`, transactions) for a made-up
application id with "Application does not exist", so the token path works and
the error shows cleanly on the panel.

- **Files:**
  - `flight_deck/bank_statements.py`: the AIS client, the Revolut source, and
    reducing every response to outgoing rows.
  - `flight_deck/statements_routes.py`: routes, sync, slots, PSU headers.
  - `flight_deck/payments_jobs.py`: the loop; it also runs `expire_stale`, which the routes still run too.
  - `payments/matching.py`: pure helpers (amount, reference digits,
    fingerprint).
  - `components/payments/StatementsPanel.tsx`.
- **Routes:**
  - `GET /fd/payments/statements`, `GET …/aspsps`, `POST …/connect`.
  - `GET …/callback` (public).
  - `PUT …/accounts/{uid}`, `POST …/sync`, `DELETE …/{provider}/{session_id}`,
    `PUT …/revolut`.
  - The router is included before the payments router, because
    `/fd/payments/{id}` would otherwise catch these paths.
- **Where it differs from the plan above:**
  - **Accounts and transactions are keyed by Enable Banking's
    `identification_hash`**, not `uid`: a renewal gets new uids. Reconnecting
    the same bank is a renewal:
    - the accounts keep their watch choice and sync point;
    - the old session is deleted, both here and at Enable Banking;
    - stored transactions carry over without duplicates, since
      `entry_reference` is stable for the same hash.
  - **The first read goes 13 months back** (`strategy=longest`), not 90 days.
    It runs right after the callback, while the user is present. That fits
    decision 2 (yearly bills), and a fresh SCA lets banks give more than 90
    days. The callback page waits up to 20 seconds for it and says how many
    outgoing payments it read.
  - **The unattended limit is a rolling 24 hours** (the list
    `bank_sessions.unattended_at`), not a calendar day.
  - **A failed read still uses its slot** (`last_attempt_at`), so a broken
    bank or application can't burn the day's four reads in 20 minutes.
  - **An owner with nothing to confirm** gets at most one read a day, which
    keeps the history fresh for 4d.
  - **The Revolut source reads transfers only.** The payee IBAN comes from
    `GET /counterparty/{id}` (the leg's `counterparty.account_id` picks the
    account), looked up once per sync.
  - **`FD_ENABLEBANKING_STATEMENTS_REDIRECT_URL`** overrides the callback URL
    the deck works out, like Phase 2's override.

## As built: 4b (2026-10-19)

Tested with table tests of the pure matcher, end-to-end tests against a fake
Enable Banking, and on a spare deck with seeded statement rows (settle, paid
twice, a name-only suggestion rejected, a paid-before-prepared suggestion
confirmed).

- **Files:**
  - `payments/matching.py` (`grade`, `match`, `not_seen`), pure.
  - `flight_deck/statement_matching.py`: carries decisions out; the owner's
    answer.
  - Card UI in `PaymentCard.tsx`.
- **Store:**
  - A `statement_links` table: transaction ↔ bill, with state `auto`,
    `confirmed`, `pending`, `suggested`, `rejected` or `duplicate`, and a
    reason.
  - A `statement` JSON column on bills: `settled_by`, `pending` and
    `suggestions`, so list views need no extra queries.
  - `bank_transactions.intent_id` / `match` are set for settling and duplicate
    links, which keeps those rows past the retention purge.
- **When matching runs:**
  - after every statement sync;
  - when a bill is prepared (it may be paid already);
  - after "I paid" on the card: a read with the user present, skipped if the
    statement was read in the last 10 minutes or the bank needs PSU headers
    Flight Deck can't give;
  - hourly from the loop, so "not on the statement" ages.
- **Routes:**
  - `POST /fd/payments/{id}/statement-match {fp, accept}` (owner).
  - `PUT /fd/payments/statements/settings {not_on_statement}`.
  - `GET /fd/payments/{id}` adds `statement_suggestions`.
- **The agent** gets `statement`: booked on, account label, match, pending
  since, and a suggestion count. No creditor names, amounts or fingerprints.
  The tool says "BANK CONFIRMED — on the user's statement, booked …".
- **Where it differs from the plan above:**
  - **References are matched on their digits alone** (at least 4), so a bank
    that drops the model still matches. HR99 means no reference.
  - **A bill without a reference** settles automatically only when it is the
    only open bill with that IBAN and amount, and only with a payment booked
    no earlier than a day before the bill was prepared. An older one belongs
    to the last period.
  - **An open bill wins a transaction over a paid one.** A paid bill only
    claims a transaction from 3 days before to 20 days after its first
    payment, as evidence or as "paid twice". So next month's rent isn't
    "paid twice".
  - **`paid_before_prepared` is blocking**, not info. The bill moves to
    `needs_review`, so it can't be handed over until the owner confirms or
    rejects that payment, or acks the warning.
  - **A bill the user marked paid keeps its paid time and method** when the
    statement settles it.
  - **`not_on_statement`** needs every watched account of an active connection
    read past the payment day plus 4 business days. It has an on/off switch in
    the Bank statements panel.
  - **Paid-bill warnings stay visible:** `paid_twice`, `bank_reversed` and
    `not_on_statement` still show on a closed bill's card.
- **Risk:** if a bank changes a booked transaction's `entry_reference`, the
  same payment could look like "paid twice". The warning says to check the
  statement, and nothing is ever unsettled.

## Goal

Today a barcode payment counts as paid only when the user says "I paid".
Phase 4 lets Captain see the payment on the user's own bank statement and mark
the bill `settled` itself. Then Captain uses what it knows (due dates, what was
paid before, and when) for two more things:

- It reminds the user before a bill is due.
- It recognises recurring bills, including ones that haven't arrived yet.

Four steps, each its own commit on `feat/payments-phase0`:

| Step | What | Needs |
|---|---|---|
| **4a** | Statements: connect a bank account for reading, sync outgoing payments | Enable Banking AIS (the same app as Phase 2) or the existing Revolut Business connection |
| **4b** | Matching: settle bills automatically on an exact match; suggest on a near match; flag double payments | 4a |
| **4c** | Due-date reminders: bell + the chat the bill came from | Phase 0 only (4a/4b make it smarter) |
| **4d** | Recurring bills: detect, confirm, prepare or watch for the next one | Phase 0 history; 4a adds bills paid outside Captain |

Why this beats Grok: Grok's bot can pay with a card it was given. Captain
already prepares the bill so the user pays in their own bank. With Phase 4 it
also knows the bill was really paid, and that it was paid only once. It notices
when this month's bill hasn't arrived yet. And it never holds a card or a bank
login.

## Ground rules (Phase 4)

1. **Read only.** Account information can't move money, and nothing in Phase 4
   pays. Recurring bills are prepared the same way as any bill: the user still
   pays in their bank.
2. **The agent never sees the statement.** There's no agent route for
   transactions, balances or accounts. The model gets one line per bill, for
   example: "bank confirmed: booked 24.10. on Erste ••1234".
3. **Keep the least.** Only outgoing (DBIT) transactions are kept, with only
   the fields matching needs (listed in the store section below).
   - Incoming payments are dropped when the response is parsed and never
     written. Balances are never fetched.
   - Rows go after 13 months, on disconnect, and when the owner is deleted.
4. **Auto-settle only on an exact match.** Anything less certain is a
   suggestion the owner confirms on the card. Nothing is ever auto-unsettled:
   a reversal raises a warning instead.
5. **One bank transaction settles at most one bill.**
6. **Reminders are Flight Deck's own fixed text.** No model writes them. They
   respect quiet hours, go out at most once a day per user (as one digest), and
   can be switched off per bill or entirely.
7. **Recurring bills act only after the owner confirms them.** A detected
   pattern is a suggestion until then.

## The providers and their rules

| | Enable Banking AIS | Revolut Business (Phase 1 connection) |
|---|---|---|
| Banks | Most EU banks: Zagrebačka, PBZ, Erste, RBA… (**no OTP banka**, as in Phase 2) | The connected Revolut Business account |
| Consent | The user approves read access at their bank (SCA). It lasts up to the bank's `maximum_consent_validity` (at most 180 days under the amended RTS), then needs renewing | Already granted (scope `READ` is in `READ,WRITE`); no new consent |
| Cost / licence | **Restricted production is free, but only for accounts linked in the Enable Banking control panel of the account that owns the application.** That fits the deck owner's own accounts. Other users need the production contract (AIS under Enable Banking's licence), the same business step as Phase 2 | Included |
| Frequency | PSD2 RTS Art. 36(5)(b): **at most 4 unattended reads per account per 24 h**. Reads made while the user is present (with PSU headers) don't count | Revolut's normal API rate limits |

### Enable Banking API used

Verified against enablebanking.com/docs/api/reference on 2026-10-19:

- **`POST /auth`**
  - Body: `{access: {valid_until}, aspsp: {name, country}, state, redirect_url, psu_type, language}`.
  - Returns `{url, authorization_id}`. The user opens `url` and authorises at the bank.
- **Redirect back:** `redirect_url?code=…&state=…`, or `&error=…` on failure.
- **`POST /sessions {code}`**
  - Returns `{session_id, accounts: [...], aspsp, psu_type, access: {valid_until}}`.
  - Account fields: `uid`, `account_id {iban}`, `name`, `currency`,
    `cash_account_type`, `identification_hash`. The `uid` is what transaction
    calls take.
- **`GET /sessions/{id}`** returns the session `status` and `access`.
  **`DELETE /sessions/{id}`** revokes it.
- **`GET /accounts/{uid}/transactions`**
  - Query: `date_from`, `date_to`, `continuation_key`, `strategy`
    (`default` | `longest`).
  - Returns `{transactions, continuation_key}`; keep paging while a key comes back.
- **Optional `Psu-Ip-Address` / `Psu-User-Agent` headers** on data calls mark
  the user as present.
  - `aspsp.required_psu_headers` lists the headers that bank requires.
  - It's all of them or none.
- **Transaction fields used:**

  | Field | Use |
  |---|---|
  | `credit_debit_indicator` | Only DBIT is kept |
  | `status` | BOOK settles; PDNG shows "pending at your bank"; RJCT/CNCL mean a reversal |
  | `transaction_amount {amount, currency}` | Matching |
  | `booking_date`, `value_date`, `transaction_date` | Matching and display |
  | `creditor {name}`, `creditor_account {iban}` | Matching |
  | `reference_number` (+ `reference_number_schema`), `remittance_information[]` | Matching |
  | `entry_reference` | Stable per account; part of the fingerprint |

  `transaction_id` is **not** stable between reads, so nothing keys on it.
- **Errors:**
  - `EXPIRED_SESSION`, `REVOKED_SESSION`, `CLOSED_SESSION`: the connection
    needs renewing.
  - `ASPSP_RATE_LIMIT_EXCEEDED`: wait for the next sync slot.

### Revolut API used

- **`GET /transactions?from&to&account&count&type&state`**
  - Newest first, up to 1,000 per page; page by passing the last item's
    `created_at` as `to`.
  - Fields: `id`, `type`, `state`, `completed_at`, `reference`, `legs[]`.
  - The exact counterparty fields on a leg are to be confirmed in 4a.
- **`POST /payment-drafts`** accepts `schedule_for` (a date). Used only in 4d,
  opt-in.

## Part A: statements and matching (4a, 4b)

### How it works

1. **Deck setup (admin, once).**
   - Reuse `FD_ENABLEBANKING_APP_ID` / `FD_ENABLEBANKING_KEY` from Phase 2.
   - Add `<deck>/fd/payments/statements/callback` to the application's allowed
     redirect URLs.
   - `FD_PAYMENTS_STATEMENTS=1` turns the feature on.
   - In restricted mode, the admin links their own accounts in the Enable
     Banking control panel first. Otherwise the session comes back with no
     accounts, and the panel explains why.
2. **The user connects (once per bank, renewed when consent runs out).**
   - Payments page → **Bank statements** → bank (defaults to the Phase 2 bank
     choice) → Connect.
   - The user authorises at the bank (SCA) and comes back to the callback.
   - They pick which accounts to watch; EUR current accounts are pre-selected.
   - Consent wording: *"Captain reads outgoing payments from these accounts to
     confirm the bills you paid. It doesn't keep incoming payments or balances,
     and it can't move money."*
3. **Revolut Business users** get a "Use my Revolut statement too" switch on
   the existing Revolut panel. No new consent is needed.
4. **Sync.**
   - **Background:** twice a day (`FD_PAYMENTS_SYNC_TIMES`, default
     `07:30,16:30` deck time). That leaves 2 of the 4 unattended reads a day
     for retries. It runs only for owners with something to match (a live or
     "I paid" bill, or an active recurring bill).
   - **While the user is present:** a "Check now" button, the "I paid" tap,
     and opening a bill card older than 6 hours since the last sync. These
     calls carry the user's IP and user agent as PSU headers, so they don't use
     the unattended budget. At most once a minute.
   - **Window:** the first sync looks back 90 days. Later syncs start 5 days
     before the newest booking date already seen, because banks book late.
     Pages are followed through `continuation_key`.
5. **Matching** runs after every sync (rules below). Exact matches settle the
   bill; anything else becomes a suggestion or a warning.
6. **Renewal.** A bell 7 days before `valid_until`, another on expiry. Renew
   runs the same connect flow.

### Matching rules (4b)

**Normalising:**
- **IBAN:** the existing `iban.normalize`.
- **Amount:** exact cents, EUR only.
- **Reference:** the model plus the reference digits ("HR01 7269-68499637766-00019"
  → `01 7269 68499637766 00019` → `0172696849963776600019`). It's searched
  for in `reference_number` and in the digits of the joined
  `remittance_information`.
- **Payee name:** the existing `payee_key`, compared loosely (a token overlap)
  with `creditor.name`.

**Candidate bills:** `ready`, `handed_off`, `needs_review`, `user_paid`, and
`expired` (paid late). Bills that are `settled`, `skipped` or `cancelled` are
never candidates.

| Tier | When | What happens |
|---|---|---|
| **Exact** | Same amount, **and** the same creditor IBAN, **and** the same reference. If the bill has no reference: it's the only candidate with that IBAN and amount. **And** booked no earlier than the bill was prepared minus 1 day | **Auto `settled`**, `paid_via=statement`. An event records the transaction fingerprint |
| **Probable** | Same amount, and *one* of IBAN or reference matches while the other is missing on the bank's side (some banks omit the creditor IBAN on outgoing transfers). Or: same amount + reference + similar payee name | **Suggestion** on the card: "Is this your payment? 84.20 EUR to HEP ELEKTRA, booked 24.10." → [Yes, that's it] [No] |
| **Paid before Captain** | An exact match booked *before* the bill was prepared | Suggestion plus an info warning `paid_before_prepared`: "You seem to have paid this on 02.10., before Captain prepared it — don't pay it again." |
| None | Anything else | Nothing |

**Ambiguity:**
- **One transaction, two candidates** (two bills from the same payee with the
  same amount and no reference): suggestion, never automatic.
- **One bill, two matching transactions:** the first settles it. The second
  raises a **`paid_twice`** warning and a bell: "HEP 84.20 EUR left your
  account twice: 24.10. and 25.10." The user can ask their bank to recall it.

**Statuses:**
- **PDNG:** the card shows "pending at your bank (24.10.)"; no status change.
- **RJCT / CNCL** on a transaction that settled a bill: a **`bank_reversed`**
  blocking warning on the bill, and a bell. The bill stays settled until the
  owner acts.
- **"I paid" with no transaction after 4 business days**, with every watched
  account synced since: info warning **`not_on_statement`** ("Captain can't see
  it on Erste ••1234 — paid from another account? You can ignore this.").
  Decision 4 below.

**Other rails:**
- **Phase 2 bank links:** a final PIS status already settles the bill. A
  statement match then adds the booking as evidence. A link stuck on an
  accepted-but-not-final status is settled by the statement.
- **Phase 1 Revolut drafts:** the draft status already settles them. The
  Revolut statement source matches barcode bills the user paid by hand in the
  Revolut app.

### Store (`payments_store.py`)

Three new tables:

```sql
-- One read-only bank connection (Enable Banking session or the Revolut statement source).
CREATE TABLE IF NOT EXISTS bank_sessions (
    owner_id        TEXT NOT NULL,
    provider        TEXT NOT NULL,            -- 'enablebanking' | 'revolut'
    session_id      TEXT NOT NULL,            -- EB session id; 'revolut' for Revolut
    aspsp_name      TEXT NOT NULL DEFAULT '',
    aspsp_country   TEXT NOT NULL DEFAULT '',
    psu_type        TEXT NOT NULL DEFAULT 'personal',
    valid_until     REAL,
    status          TEXT NOT NULL,            -- active | needs_renewal | revoked
    last_sync_at    REAL,
    unattended_day  TEXT NOT NULL DEFAULT '', -- YYYY-MM-DD of the counter below
    unattended_used INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    PRIMARY KEY (owner_id, provider, session_id)
);
CREATE TABLE IF NOT EXISTS bank_accounts (
    owner_id        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    uid             TEXT NOT NULL,
    label           TEXT NOT NULL,            -- "Erste ••1234" (the full IBAN isn't stored)
    currency        TEXT NOT NULL DEFAULT 'EUR',
    watched         INTEGER NOT NULL DEFAULT 1,
    synced_through  TEXT NOT NULL DEFAULT '', -- newest booking_date seen
    PRIMARY KEY (owner_id, uid)
);
-- Outgoing transactions only.
CREATE TABLE IF NOT EXISTS bank_transactions (
    owner_id         TEXT NOT NULL,
    account_uid      TEXT NOT NULL,
    fp               TEXT NOT NULL,           -- sha256(account uid | entry_reference) or of the fields when absent
    status           TEXT NOT NULL,           -- BOOK | PDNG | RJCT | CNCL | …
    booking_date     TEXT NOT NULL DEFAULT '',
    value_date       TEXT NOT NULL DEFAULT '',
    amount_cents     INTEGER NOT NULL,
    currency         TEXT NOT NULL,
    creditor_name    TEXT NOT NULL DEFAULT '',
    creditor_iban    TEXT NOT NULL DEFAULT '',
    reference_digits TEXT NOT NULL DEFAULT '',
    remittance       TEXT NOT NULL DEFAULT '', -- joined, at most 140 chars
    intent_id        TEXT NOT NULL DEFAULT '',
    match            TEXT NOT NULL DEFAULT '', -- auto | confirmed | suggested | rejected
    seen_at          REAL NOT NULL,
    updated_at       REAL NOT NULL,
    PRIMARY KEY (owner_id, fp)
);
```

**Intent columns (migrations):**
- `settled_by` (JSON `{fp, booked_on, account_label, match}`).
- `notify_origin` (4c).
- `reminders` (4c).
- `recurring_id` (4d).

**Retention:**
- Unmatched rows go after 400 days. That's long enough to spot yearly bills
  (decision 2).
- Matched rows live as long as their bill.
- Disconnect deletes the session's rows. A settled bill keeps its `settled_by`
  summary.
- `delete_owner` deletes everything.

### Code

| File | What |
|---|---|
| `flight_deck/bank_statements.py` (new) | Enable Banking AIS client: `start_auth`, `create_session`, `session_status`, `delete_session`, `transactions` (paging, PSU headers, error mapping onto `BankLinkError`), reusing `bank_link.auth_token` / `_request`. Plus the Revolut statement source over `RevolutClient`. Every response is reduced to outgoing rows when parsed |
| `captain_claw/payments/matching.py` (new, pure) | `normalize_reference`, `fingerprint`, `match(intents, transactions) -> [Decision]` with the tiers above. No I/O, so it's table-tested |
| `flight_deck/statements_routes.py` (new) | Routes below |
| `flight_deck/payments_jobs.py` (new) | `payments_loop(stop)`: one background loop for all of Phase 4 (see below) |
| `payments_routes.py` | The view gains `statement` (`booked_on`, `account_label`, `match`, `pending`) and `suggestion`. `expire_stale` moves into the loop |
| `server.py` | Start `payments_loop` in lifespan when `FD_PAYMENTS=1`, next to `scheduler_loop` |

### Routes

Owner:
- `GET /fd/payments/statements`: configured, sessions with accounts, last
  sync, next sync, valid until, error.
- `POST /fd/payments/statements/connect {aspsp_name, country, psu_type}` → `{url}`.
  It uses a one-time `state`, like Phase 2.
- `GET /fd/payments/statements/callback` (public, found by `state`).
- `PUT /fd/payments/statements/accounts/{uid} {watched}`.
- `POST /fd/payments/statements/sync`: user-present sync; PSU headers come from
  the request.
- `DELETE /fd/payments/statements/{session_id}`: revokes at Enable Banking and
  deletes the rows.
- `POST /fd/payments/{id}/statement-match {fp, accept}`: confirm or reject a
  suggestion.

Agent: no new route. `get` and `list` carry the statement line.

### UI

- **Bank statements panel** on the Payments page, styled like `BankLinkPanel`:
  - connect, accounts with watch checkboxes, last and next sync;
  - "Check now", renew, disconnect;
  - a restricted-mode explanation when no accounts come back.
- **PaymentCard:**
  - a "Bank confirmed · booked 24.10. · Erste ••1234" line;
  - the suggestion box (Yes / No);
  - the `paid_twice`, `bank_reversed` and `not_on_statement` warnings.
- **List:** the Paid tab tells "bank confirmed" from "you said paid".

### Agent tool

- **`get`:** a settled bill reads "BANK CONFIRMED — booked 24.10. on Erste
  ••1234 (paid_via statement)". A pending one reads "the bank shows it pending
  (24.10.)".
- **Description:** "Paid" claims still need `status: user_paid|settled`, as the
  fabrication guard already requires. The tool never lists bank transactions.

## Part B: due-date reminders (4c)

**What's due:** live bills (`ready`, `handed_off`, `needs_review`) with a
`due_date`, plus expected recurring periods (4d).

**When** (defaults are a per-user setting on the Payments page; decision 3):

| Stage | When |
|---|---|
| Before | 3 days before the due date |
| Due | On the due date |
| Overdue | 1 day after, then weekly until the bill expires (30 days after its due date) or the user skips it |

- Reminders go out at **09:00 deck time**, outside `FD_SCHEDULER_QUIET_HOURS`.
  A reminder caught in quiet hours goes out when they end.
- **One digest per user per day:** "2 bills due: HEP Elektra 84.20 EUR tomorrow
  (25.10.) · Čistoća 18.75 EUR overdue since 20.10." Each bill line has its own
  link.
- **Before sending,** if statements are connected and the last sync is older
  than 6 hours, the loop syncs first, within the unattended budget. Bills paid
  meanwhile drop out of the digest.

**Where:**
- **Always** the bell: `add_notification(type="payment", ref_type="payment", ref_id=…)`.
- **The chat the bill came from:**
  - When the agent prepares a bill in a WhatsApp or Telegram conversation,
    the tool sends the session's origin (`origin.get_session_origin`). It's
    stored as `notify_origin`.
  - The digest goes there through `delivery_routes.deliver_to_origin`. That
    path already enforces the WhatsApp allowlist and mute.
- **Bills from mail triage or cron** go to the origin of the user's most recent
  bill prepared in a chat. With none, they go to the bell only.

**Links:**
- Each line carries the bill's `/p/<token>` phone page, with its barcode and
  "I paid".
- When the page token has expired, the loop mints a new one through the same
  code as handoff. The status doesn't change, and only one live token exists
  per bill.

**Stopping:**
- **Per bill:** "Don't remind me" on the card and on the `/p` page.
- **For everything:** a global switch.
- **From chat:** the agent's new `remind` action (`id`, `on|off`). Reminders
  aren't money, so the agent may switch them; the change is logged as an event.

**Replies:**
- The digest isn't part of the agent's conversation. So the tool description
  adds: "If the user says they paid a bill they were reminded about, use list
  status=due and mark that one."
- `mark` already takes "paid" from the user's own message, not from an
  argument.

**Store:**
- Intent columns `notify_origin` (JSON) and `reminders`
  (`{stage: sent_at, off: bool}`).
- Pref `reminders`: `{enabled, days_before, time, overdue_weekly}`.

## Part C: recurring bills (4d)

### Detecting

Signals, strongest first:

1. **The user says so:** "This repeats" on a bill card, or asking the agent,
   which proposes a suggestion.
2. **Captain's own history:** at least 2 paid bills to the same payee
   (`payee_key` + IBAN) spaced ~30 days (monthly), ~61 (bimonthly), ~91
   (quarterly) or ~365 (yearly), ±5 days.
3. **The statement (4a):** outgoing transfers to the same creditor IBAN with
   the same spacing. That takes at least 3 for monthly, or 2 for yearly. This
   also finds bills paid outside Captain: standing orders, direct debits, bills
   paid on paper.

From the history, each pattern also gets:
- **Amount:** `fixed` (every payment equal) or `variable` (keeps min, max and
  last).
- **Reference:** `fixed` (rent, kindergarten, a loan: the same poziv na broj
  every time) or `per_bill` (utilities: the invoice number changes).
- **Anchor day:** the usual day of the month.

A detected pattern is a **suggestion** on the Payments page, for example:
"Track Čistoća as a monthly bill? Usually around the 20th, 18.75 EUR."
→ [Track it] [Dismiss]. Dismissed patterns aren't suggested again.

### What an active recurring bill does

| Kind | Captain does |
|---|---|
| **Fixed amount + fixed reference** | **Prepares next period's bill** 5 days before the expected due date, as an automated intent with the bell "Prepared from your recurring bill". The normal checks and reminders apply; the user pays as usual. **Optional, per bill, needs Revolut:** a Revolut payment draft with `schedule_for` = the due date. It's still approved in the Revolut app (decisions 5 and 6) |
| **Per-bill reference or variable amount** | **Waits for the invoice.** When mail triage or the agent prepares a bill from this payee, Flight Deck links it to the period. The existing `amount_unusual` check uses this bill's own history. **If nothing has arrived by the usual day + 5:** a "missing bill" line in the digest ("HEP's bill usually comes around the 10th — not seen yet. Check e-mail or the HEP portal?") |
| **Any kind, paid outside Captain** | A statement transfer that fits the period marks it covered ("paid by direct debit on 05.10."). No reminder, no prepared bill |

### Store

```sql
CREATE TABLE IF NOT EXISTS recurring_bills (
    id             TEXT PRIMARY KEY,           -- rec_<hex>
    owner_id       TEXT NOT NULL,
    status         TEXT NOT NULL,              -- suggested | active | paused | dismissed
    source         TEXT NOT NULL,              -- user | history | statement
    payee_name     TEXT NOT NULL,
    payee_key      TEXT NOT NULL,
    iban           TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    reference      TEXT NOT NULL DEFAULT '',   -- only when reference_kind = fixed
    reference_kind TEXT NOT NULL,              -- fixed | per_bill
    amount_kind    TEXT NOT NULL,              -- fixed | variable
    amount_cents   INTEGER NOT NULL DEFAULT 0, -- fixed amount, or the last one
    amount_min     INTEGER NOT NULL DEFAULT 0,
    amount_max     INTEGER NOT NULL DEFAULT 0,
    cadence        TEXT NOT NULL,              -- monthly | bimonthly | quarterly | yearly
    anchor_day     INTEGER NOT NULL,
    next_due       TEXT NOT NULL DEFAULT '',
    auto_prepare   INTEGER NOT NULL DEFAULT 1,
    revolut_draft  INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recurring_periods (
    recurring_id   TEXT NOT NULL,
    period         TEXT NOT NULL,              -- 2026-11 (or 2026-Q4, 2026)
    status         TEXT NOT NULL,              -- expected | prepared | paid | missing | skipped
    intent_id      TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',   -- e.g. "direct debit 05.10."
    updated_at     REAL NOT NULL,
    PRIMARY KEY (recurring_id, period)
);
```

### Code, routes, UI, tool

- **Code:**
  - `captain_claw/payments/recurring.py` (new, pure): `detect(history,
    transactions) -> [Pattern]`, `next_due(pattern, after)`, `period_of(date,
    cadence)`.
  - The loop does the rest.
- **Routes:**
  - `GET /fd/payments/recurring`.
  - `PUT /fd/payments/recurring/{id}` (status, auto_prepare, revolut_draft,
    cadence/day fixes).
  - `POST /fd/payments/{id}/recurring` ("this repeats").
  - `DELETE /fd/payments/recurring/{id}`.
- **UI:** a **Recurring bills** section on the Payments page:
  - suggestions with Track / Dismiss;
  - active bills with next due, amount, period history chips (paid, missing,
    covered), toggles and pause.
- **Tool:**
  - `list status=due` includes expected periods ("Čistoća expected ~20.10.,
    not prepared yet").
  - A new `recurring` action lists active bills.
  - The agent can't activate or change a recurring bill. A user's "this is
    monthly" becomes a suggestion through `POST …/recurring`, which the owner
    confirms.

## The background loop

`flight_deck/payments_jobs.py`, `payments_loop(stop_event)`. It's started in
the server lifespan when `FD_PAYMENTS=1`, the same way as
`fd_scheduler.scheduler_loop`.

- **Every 5 minutes, per owner with payments data:**
  1. Expire stale bills (today's `expire_stale`, moved here).
  2. Run a statement sync if a slot is due and budget remains (4a).
  3. Match (4b).
  4. Roll recurring periods forward: prepare fixed bills, flag missing ones (4d).
  5. Send the reminder digest once its time has come (4c).
- **It's idempotent:** each step records what it did (period status, reminder
  stage, sync slot), so restarts and double ticks don't repeat anything.
- **It's failure-isolated:** one owner's or one bank's error is logged (counts
  only, never transaction rows) and stored on the session. It never stops the
  loop.

## Flags and settings

| Setting | Default | Meaning |
|---|---|---|
| `FD_PAYMENTS_STATEMENTS` | off | Part A on (needs `FD_ENABLEBANKING_*`; the Revolut source needs only a Revolut connection) |
| `FD_PAYMENTS_SYNC_TIMES` | `07:30,16:30` | Unattended sync slots, deck time (at most 4) |
| Pref `reminders` | on: bell + chat, 3 days before, due day, overdue | Part B, per user (decision 3) |
| Pref `statement_warnings` | on | `not_on_statement` (decision 4) |
| Recurring | suggestions on | Nothing acts until the owner confirms a bill |

## Tests

- **Fake Enable Banking AIS** (`httpx.MockTransport`):
  - `/auth`, `/sessions`, transactions in two pages with `continuation_key`;
  - `EXPIRED_SESSION` and `ASPSP_RATE_LIMIT_EXCEEDED`;
  - a response with incoming rows, to prove they're never stored.
- **`matching.py`, table-driven:**
  - an exact match;
  - a bank that omits the creditor IBAN (probable);
  - HR01 written with dashes, spaces or none;
  - RF references;
  - an amount 1 cent off (none);
  - two candidates;
  - paid twice;
  - PDNG;
  - a reversal after settling;
  - paid before being prepared;
  - an expired bill paid late;
  - a bank-link bill already settled.
- **Budget:**
  - unattended reads stay at or below 4 per account per day, retries included;
  - user-present syncs send PSU headers and don't count.
- **Reminders:**
  - stage dates and quiet hours;
  - one digest per user per day;
  - per-bill and global opt-out;
  - no reminder after a statement settled the bill;
  - a fresh `/p` token when the old one expired;
  - `deliver_to_origin` called with the stored origin;
  - bell only with no origin.
- **Recurring:**
  - monthly and yearly detection, ±5 days;
  - fixed vs variable amounts;
  - nothing happens before confirmation;
  - auto-prepare runs once per period;
  - the missing-bill nudge comes once;
  - a direct debit covers the period;
  - a Revolut draft with `schedule_for` only when opted in.
- **Isolation:** agent routes never return transaction data. A test checks
  every agent response for an unmatched transaction's creditor name and
  remittance text.
- **Regression:** the full payments suite as before, plus `test_card_scrub.py`
  and `test_fabrication_guard.py`.

## Order of work

1. **4a:** store tables and migrations; `bank_statements.py`; routes; panel;
   the loop's sync step; the Revolut statement source.
2. **4b:** `matching.py`; settle, suggest and warning flows; card UI; the
   tool's statement line.
3. **4c:** origin capture in `prepare`; reminder prefs; the digest step;
   `/p` token refresh; `remind` action; settings UI.
4. **4d:** `recurring.py`; tables; suggestions UI; auto-prepare,
   missing-bill nudge and coverage by statement; optional Revolut scheduled
   drafts.
5. **Live check (needs you):**
   - Link your own Croatian account in the Enable Banking control panel
     (restricted mode).
   - Run 4a/4b on a spare deck against real statements, and tune the matcher
     to what your bank actually sends.

## Not verified / risks

- **What Croatian banks put in outgoing transactions:** whether
  `creditor_account.iban`, the HR reference, and `entry_reference` come
  through, which differs by bank. When they're missing, the matcher falls back
  to suggestions. The first live sync will show.
- **Booking delay:** instant transfers usually book the same day, other SEPA
  transfers by the next business day. The `not_on_statement` warning waits 4
  business days for this reason.
- **Restricted mode only covers the Enable Banking account holder's own linked
  accounts.** Statements for other deck users need the production contract.
- **OTP banka** isn't supported by Enable Banking, as in Phase 2.

## Out of scope for Phase 4

- Balances ("is there enough money?"), spending categories, budgets.
- Incoming payments (for example, invoices the user issued).
- Direct-debit mandates, and standing orders via PSD2 periodic payments.
- Webhooks: syncs are scheduled or on demand.
- Any agent access to statements.

## Decisions (agreed 2026-10-19: all as recommended)

1. **Who can connect statements:**
   - **Recommended:** the deck's Enable Banking app now. In restricted mode
     that's the app owner's linked accounts; others get access once there's a
     production contract.
   - Alternative: also let each user bring their own Enable Banking app (their
     own restricted mode).
2. **Unmatched outgoing transactions:**
   - **Recommended:** keep them 13 months, so recurring and yearly bills can be
     found from the statement.
   - Alternative: keep only matched ones; recurring bills then learn only from
     Captain's own history.
3. **Reminder defaults:**
   - **Recommended:** on, bell + the chat the bill came from, 3 days before /
     due day / 1 day after, 09:00, one daily digest.
4. **`not_on_statement`:**
   - **Recommended:** on, as an info warning after 4 business days.
   - Alternative: off.
5. **Fixed recurring bills:**
   - **Recommended:** auto-prepare 5 days before the due date, once the owner
     has confirmed the bill.
   - Alternative: remind only.
6. **Revolut scheduled drafts for fixed recurring bills:**
   - **Recommended:** opt-in per bill.
   - Alternative: off entirely.

## References

- **Enable Banking API reference**, enablebanking.com/docs/api/reference
  (checked 2026-10-19): `/auth`, `/sessions`, `/accounts/{id}/transactions`,
  the Transaction schema, PSU headers, `maximum_consent_validity`, and the
  `EXPIRED_SESSION` / `ASPSP_RATE_LIMIT_EXCEEDED` errors.
- **Firefly III's Enable Banking guide**,
  docs.firefly-iii.org/tutorials/data-importer/eb: in restricted mode only
  accounts linked in the Enable Banking control panel are returned.
- **PSD2 RTS:**
  - Delegated Regulation (EU) 2018/389, Art. 36(5)(b): at most 4 unattended
    account reads per day.
  - Amended by (EU) 2022/2360: 180-day AIS re-authentication.
- **Revolut Business API** (developer.revolut.com, checked 2026-10-19):
  - `POST /payment-drafts` with `schedule_for` (date).
  - `GET /transactions` (`from`, `to`, `account`, `count`, `type`, `state`;
    fields `id`, `type`, `state`, `completed_at`, `reference`, `legs`).
