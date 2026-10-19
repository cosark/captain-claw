# Payments, Phase 1: Revolut Business payment drafts

Status: built on branch `feat/payments-phase0` (2026-10-19), tested against a
fake Revolut. **Not yet run against a real Revolut Business (sandbox or
production) account.** Builds on Phase 0 (docs/payments-phase0-plan.md); same
ledger, same warnings.

## As built (notes)

- **Routes:**
  - Owner, under `/fd/payments/revolut/`: `status`, `key`, `client`,
    `connect`, `code`, `accounts`, `account`, and `DELETE connection`.
  - Public: `GET /fd/payments/revolut/callback`.
  - `POST /fd/payments/{id}/revolut` is the card's "Send to Revolut".
  - The agent's `handoff` takes `rail`. `status`, `prepare` and `get` report
    `rails` (`barcode`, plus `revolut` once connected with an account picked).
- **Names:** the counterparty and the VoP check always use `company_name`. A
  bill's payee can't be told apart from a person reliably, and VoP compares
  names, not types.
- **Local decks:** the redirect URI is the deck's own origin. If Revolut
  refuses an `http://localhost` redirect URI, either use the deck's public
  https address, or let the page show the code and paste it on the Payments
  page within 2 minutes.
- **New status `failed`:** terminal, not paid. Prepare the payment again to
  retry.
- **The Phase 0 ledger migrates in place:** `handoff_rail` and `rails_state`
  are added to an existing `payments.db`. Checked on the Phase 0 test deck.

## Goal

For a user with a Revolut Business account, Captain puts the prepared payment
straight into Revolut Business as a **payment draft**. The user opens the
Revolut Business app, checks it, and sends it. Captain sees when it completes
and marks the payment **settled** (bank-confirmed), which Phase 0 couldn't do.

## Ground rules (on top of Phase 0's)

1. **Captain's Revolut token can't pay.** It asks for scope `READ,WRITE` only.
   `WRITE` lets it create counterparties and payment drafts; `PAY` (initiate
   transfers) is never requested, so Revolut itself refuses a payment from
   Captain even if the token leaked.
2. **The draft is the handoff.** A draft moves no money. The user sends it in
   the Revolut Business app, and Revolut's own approval rules and 2FA apply.
3. **The same gates as a barcode.** A draft is made only for a `ready` (or
   already handed over) payment with no open blocking warning.
4. **The payee's name is checked first.** Before the draft, Revolut's Verification of
   Payee (VoP, mandatory for EUR since Oct 2025) compares the payee name with
   the IBAN:
   - **No match / close match:** a blocking warning, cleared only by the owner.
   - **Can't be checked:** a note, and the draft goes ahead.

## Revolut Business API (verified against developer.revolut.com, 2026-10-19)

| What | Detail |
|---|---|
| Base URL | `https://b2b.revolut.com/api/1.0` · sandbox `https://sandbox-b2b.revolut.com/api/1.0` |
| Consent | `https://business.revolut.com/app-confirm?client_id=…&redirect_uri=…&response_type=code&scope=READ,WRITE` (sandbox: `sandbox-business.revolut.com`). The code is valid for 2 minutes |
| Client assertion | RS256 JWT: `iss` = redirect URI's domain (no scheme), `sub` = client_id, `aud` = `https://revolut.com`, `exp` = numeric |
| Token | `POST /auth/token`, form: `grant_type=authorization_code`, `code`, `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`, `client_assertion`. The access token lasts 40 minutes; the refresh token doesn't expire; a refresh invalidates the previous access token |
| Accounts | `GET /accounts` → `[{id, name, balance, currency, state}]` |
| Counterparties | `GET /counterparties?iban=<exact>` · `POST /counterparty` `{company_name \| individual_name, bank_country, currency, iban}` (BIC and address are optional for SEPA EUR) |
| VoP | `POST /account-name-validation` `{iban, recipient_country, recipient_currency, company_name \| individual_name{first_name,last_name}}` → `{result_code: matched \| close_match \| not_matched \| cannot_be_checked \| temporarily_unavailable, company_name/individual_name, name_validation_id}`. Sandbox always answers `cannot_be_checked` |
| Drafts | `POST /payment-drafts` `{title, payments:[{account_id, receiver:{counterparty_id, account_id}, amount, currency, reference}]}` → `{id}` · `GET /payment-drafts/{id}` → `payments[].state` ∈ CREATED, PENDING, COMPLETED, REVERTED, DECLINED, CANCELLED, FAILED, DELETED · `DELETE /payment-drafts/{id}` |

## Connecting (per user, Payments page → Revolut Business)

1. **Make a key.** Flight Deck generates an RSA-2048 key and a self-signed
   certificate. It shows the certificate to paste into Revolut Business
   (Settings → APIs → Business API → Add API certificate), and the OAuth
   redirect URI to enter there: `<deck>/fd/payments/revolut/callback`.
2. **Paste the Client ID** that Revolut shows, and choose Production or
   Sandbox.
3. **Connect.** This opens Revolut's consent page (`READ,WRITE`). Revolut
   redirects back to the callback with the code, which Flight Deck exchanges for
   tokens. The callback finds its user by a one-time `state`. If Revolut doesn't
   pass `state` back, it uses the only connect started in the last 10 minutes.
   Failing both, the page shows the code to paste on the Payments page.
4. **Pick the account to pay from.** The default is the first active EUR account.
5. **Disconnect** forgets the key and tokens. The user removes the certificate
   in Revolut.

Storage: `payment_connections` in `payments.db` (owner, provider,
environment, client_id, redirect_uri, private key, refresh/access token,
account_id). This is the same storage as Flight Deck's Google and Microsoft
tokens, so it's as safe as the deck's data directory. Because of the
`READ,WRITE` scope, a leak exposes statements and lets someone create drafts,
but never move money.

## Handing over to Revolut

`payments handoff id=… rail=revolut` (agent), or "Send to Revolut" on the card
(owner):

1. **Gate:** the payment is `ready` / `handed_off` with no open blocking
   warning, and Revolut is connected.
2. **VoP**, once per payment. Its result is kept, and a close match or no match
   adds a blocking warning and stops here.
3. **Counterparty:** look it up by exact IBAN, or create it (`company_name`,
   `bank_country` from the IBAN, `EUR`, `iban`).
4. **Draft:** title `Captain · <payee>`, reference
   `<model> <poziv na broj>` (or the RF reference, or the description), cut to
   140 characters.
   - A Croatian model reference travels as free text. An info warning says so,
     because the payee may match it more slowly than from a barcode.
5. **Record it:** the draft id goes into `rails_state.revolut`, and the status
   becomes `handed_off` (rail `revolut`).

## Status sync

The draft is read again whenever the payment is read (the owner's card, the
list, the agent's `get` / `list`):

| `payments[0].state` | Ledger |
|---|---|
| CREATED, PENDING | stays `handed_off`; the card shows "waiting in Revolut" / "pending approval" |
| COMPLETED | **`settled`** (`paid_via=revolut`) |
| DECLINED, FAILED, REVERTED | **`failed`** (new terminal status), with Revolut's reason |
| CANCELLED, DELETED, draft gone (404) | `cancelled` ("removed in Revolut") |

Skipping or cancelling a payment in Captain deletes its draft if it is still
`CREATED`.

A background poller is left out: the payment shows `settled` the next time
anyone looks at it.

## Out of scope for Phase 1

- Revolut's `/pay` (would need `PAY`)
- Scheduled drafts on the due date
- Webhooks
- Revolut personal accounts (no API; that's Phase 2, through PSD2)
- Non-EUR payments
