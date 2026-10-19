# Payments, Phase 3: Captain buys from online shops

Status: **3a, 3b and 3c built** on branch `feat/payments-phase0` (2026-10-19):
the purchase core, the Link wallet, Revolut cards per purchase, the safe
checkout, ACP checkout, and AP2 checkout. Everything is tested against fakes.
The checkout fill was also tested against real headless Chromium pages, ACP
requests against the official JSON Schema, and AP2 mandates with Google's own
AP2 SDK. Nothing has run against a live Link, Revolut, ACP or AP2 service yet.

## What changed from the original Phase 3 idea

"AP2 / card-network agent tokens" can't be used in the EU today. The research
found:

| Option | Reality (Oct 2026) |
|---|---|
| **Stripe Link Agent Wallet** (what Grok Bot uses) | Real and documented, with an MIT CLI and a Python SDK. The user approves each spend request in Link, and Link returns a one-time card, a Link Pay Token or a shared payment token. **US and Canadian customers only**, and an agent platform must apply to Stripe for an OAuth client |
| **Revolut Business virtual cards** | `POST /cards` makes a company card with a `single` + `all_time` limit equal to the purchase amount and an auto-terminate date. Reading the number needs the `READ_SENSITIVE_CARD_DATA` scope, which Revolut allows only with an IP whitelist. Still never `PAY` |
| **ACP** (OpenAI + Stripe, spec 2026-04-17) | A REST checkout the merchant hosts, paid with a delegated token (a Stripe SPT). Every merchant must onboard the agent itself; there's no open sandbox merchant |
| **AP2** (Google, v0.2 since 2026-04-28) | A2A JSON-RPC between agents with SD-JWT checkout and payment mandates. The samples need a Gemini key; there are no public merchants |

The user picked all three: the Link wallet with the safe checkout, a Revolut
card per purchase, and the protocols.

## Ground rules

1. **Nothing to pay with until the user approves this seller and amount.** For
   Link, the approval happens in Link itself. For a Revolut card (and for
   ACP/AP2), the owner approves in Flight Deck; there's no agent route that
   approves.
2. **The card never reaches the model.** Flight Deck releases the one-time
   credential once, to the agent's `checkout_pay` step.
   - That step types it into the shop's form and clicks pay **in the same
     call**, then reports only the outcome: which fields were filled (names,
     not values) and the resulting page's scrubbed text.
   - The exact card it typed is also removed from that text, even when it
     doesn't look like a card.
   - If the form is still there (declined, no button), the card fields are
     cleared.
3. **The ledger moves on the provider's word.**
   - Link's spend-request status: `succeeded` → completed.
   - Revolut's card transaction: completed → completed, and the card is
     terminated.
   - The shop's confirmation page, read by the tool and not by the model:
     `ordered`.
4. **"I bought it" needs a confirmed purchase.** The fabrication guard accepts a
   first-person purchase or payment claim only when a `payments` result shows
   `purchase_intent: pur_… status: ordered|completed`. Bills keep the Phase 0
   rule.
5. **A purchase that won't happen takes its credential with it.** The spend
   request is cancelled, or the card is terminated.

## How it works (3a)

```
agent: payments purchase(source, merchant, url, items, amount)
   → FD: purchases row, status awaiting_approval
      link:          Link spend request (context ≥ 100 chars, idempotency key = purchase id)
                     → approval_url (the user approves in Link)
      revolut_card:  the owner approves on the purchase card (Flight Deck) → FD makes the card
   → approved
agent: browser — cart, shipping, contact; stops at the card form
agent: payments checkout_pay(id, pay_button)
   tool: POST …/credential (once) → card | link_pay_token, kept in memory only
         card_fill.fill_and_pay(page, …): find fields (page + iframes), type, click pay, wait, read outcome
         POST …/result {outcome, url, title, excerpt (scrubbed)}
   → ordered | failed | needs_action (3-D Secure: the user confirms, then checkout_check)
FD sync on every read: Link status / Revolut card transactions → completed
```

## Pieces (3a)

**`payments/card_fill.py`**
- Field finders for card number, expiry (combined, or split month/year
  selects), CVC and name, across the page and every frame.
- Typing by keystroke, so the fields' own formatting runs.
- The Link Pay Token goes in through the native value setter, as Stripe
  documents.
- The pay button is found by exact text, role or CSS.
- Outcome patterns: confirmed (EN/HR/DE), declined, or needs action (3-D Secure).

**Flight Deck**
- `flight_deck/link_wallet.py`
  - OAuth: authorization code + PKCE at `login.link.com/auth` (with the
    publishable `key`). The token call carries the publishable key as bearer
    and the client secret in the form; refresh tokens rotate.
  - REST at `api.link.com`: `POST /spend_requests`,
    `GET /spend_requests/{id}?include=card|link_pay_token`, `…/cancel`,
    `/userinfo` and `/agent_observations`.
  - `FD_LINK_TEST=1` sets `test: true`, so no real charge happens.
- `flight_deck/revolut_business.py`: `create_purchase_card`, `card_details`,
  `terminate_card`, `card_payments` and `team_members`. Consent with
  `cards=True` asks for `READ,WRITE,READ_SENSITIVE_CARD_DATA`.
- `flight_deck/purchases_routes.py`
  - Agent: `POST /fd/payments/agent/purchases`, `GET …/{id}`,
    `POST …/{id}/credential` (once), `…/result`, `…/cancel`.
  - Owner: `GET /fd/payments/purchases`, `GET …/{id}`, `POST …/{id}/approve`,
    `…/deny`, `…/cancel`.
  - Link: `GET /fd/payments/link/status`, `POST /fd/payments/link/connect`,
    `GET /fd/payments/link/callback`, `DELETE /fd/payments/link/connection`.
  - `POST /fd/payments/revolut/cards` switches Revolut cards on or off.
  - Registered **before** the payments router, so that
    `/fd/payments/purchases` isn't taken as a payment id.
- `payments.db`: a `purchases` table with `credential_released` (the
  single-release guard).
- Rate limit: 20 purchases an hour. The per-user cap shows a warning on the
  approval card.

**Agent tool**
- New actions `purchase`, `checkout_pay` and `checkout_check`.
- `get` and `mark` (cancel) also take `pur_…` ids.
- The tool gets the agent's browser page from the browser tool's own session.

**UI**
- A `PurchaseCard` in chat and on the Payments page: approve or deny for
  Revolut cards, "Approve in Link" for Link. It re-reads itself while
  waiting.
- A Link wallet panel.
- "Make a Revolut card for each purchase" in the Revolut panel.
- Deep links at `/payments/purchases/<id>`.

## 3b: ACP (built)

`flight_deck/protocol_checkout.py`. The agent calls `purchase` with
`source=acp`, `protocol_endpoint` = the shop's domain, `items[].id` = the shop's
item ids, and `shipping`. Flight Deck then:

1. Discovers the shop, opens the checkout session, and picks the cheapest
   fulfillment option.
2. Asks Link for a shared payment token for the shop's total (a
   `merchant_total` note when it differs from what the agent said).
3. Once the user approves in Link, completes the checkout server-side. 3-D
   Secure becomes `needs_action`; a decline becomes `failed`.

The owner can't approve an ACP purchase in Flight Deck; only Link can. Shop
keys go in Payments → Checkout protocols, and are never returned once saved.

- Buyer side of spec 2026-04-17: `POST /checkout_sessions` (create, update,
  retrieve, complete, cancel).
- Headers: `Authorization`, `API-Version: 2026-04-17`, and an `Idempotency-Key`
  on every POST.
- Discovery through `/.well-known/acp.json`. The merchant's API key is per user,
  because every merchant onboards the agent bilaterally.
- Paid with a Link `shared_payment_token` via
  `payment_data {handler_id, instrument {type: "card", credential {type: "spt", token}}}`.
- Tested against a stub merchant built from the repo's examples, **with every
  request validated against the official JSON Schema**.

## 3c: AP2 (built)

`flight_deck/ap2_client.py`, off unless `FD_AP2=1`. Captain is the shopping
agent; `FD_AP2_SHOPPING_AGENT_ID` sets the id it presents (default
`captain-claw`; Google's samples allowlist `trusted_shopping_agent`). The owner
sets the credentials provider's A2A URL and wallet email in Payments → Checkout
protocols. Saving them makes the user's P-256 signing key, whose public key the
panel shows.

- Shopping-agent side of v0.2: A2A `message/send` (JSON-RPC) to the merchant
  agent, with the `X-A2A-Extensions` header.
- `purchase` (`source=ap2`, `protocol_endpoint` = the merchant agent's URL,
  `description` = what to look for) runs these steps:
  1. Find carts, and pick the one closest to the amount.
  2. Create the checkout in the same `contextId`.
  3. Wait for the owner's approval in Flight Deck.
- **Approve** (owner only) runs these steps:
  1. Get the shipping address, payment methods, and a credential token from
     the credentials provider.
  2. Sign the SD-JWT CheckoutMandate (`checkout_jwt` selectively disclosed) and
     the PaymentMandate (`transaction_id` = `checkout_hash`), ES256, valid for
     an hour.
  3. Send the signed mandate to the credentials provider.
  4. Initiate payment with the merchant.

  An OTP challenge becomes `needs_action` with the shop's text. The owner types
  the code on the purchase card (`POST /fd/payments/purchases/{id}/challenge`),
  never the agent. A receipt makes the purchase `completed`.
- The mandates are made by our own SD-JWT code (PyJWT). Google's AP2 SDK
  (`MandateClient().verify`) accepts the mandates from the approve flow. That
  check runs only when `AP2_VERIFY_PYTHON` points at a Python with the SDK,
  because the SDK's exact pins clash with Captain's:
  `AP2_VERIFY_PYTHON=<venv>/bin/python pytest tests/test_flight_deck/test_payments_ap2.py`.
- A live run needs the AP2 samples running, which need a Gemini API key. The
  samples' agents are LLM-driven, so the request texts follow the samples'
  wording; other merchant agents may word things differently.

## Not verified

- **Live services:** a real Link account (US/CA) with a Stripe-issued OAuth
  client, a Revolut Business account with cards and the IP whitelist, and real
  shop checkouts. Hosted fields from real payment providers may need selector
  overrides (`fields`).
- **ACP and AP2:** no public merchants or sandboxes exist. ACP was checked
  against the schema and examples only; AP2 against stub agents that follow the
  samples' sequence.
- **3-D Secure:** a challenge shown *inside* the agent's headless browser can't
  be completed by the user; only out-of-band confirmation in an app works.
