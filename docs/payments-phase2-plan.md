# Payments, Phase 2: pay-by-bank links (PSD2 payment initiation)

Status: built on branch `feat/payments-phase0` (2026-10-19) and tested against
a fake Enable Banking. **No payment has gone through Enable Banking's sandbox
yet**; that needs an Enable Banking sandbox application registered for the
deck. Builds on Phases 0 and 1 (same ledger, same warnings, same gate).

## As built (notes)

- **Routes:**
  - Owner: `GET /fd/payments/bank/status`, `GET /fd/payments/bank/aspsps`,
    `PUT`/`DELETE /fd/payments/bank/choice`, `POST /fd/payments/{id}/bank`.
  - Public: `GET /fd/payments/bank/callback`.
  - The agent's `handoff` takes `rail=bank`, and `rails` lists `bank` once the
    deck is set up and the user chose a bank.
- **The payment view** has a new `bank_link` field (`bank_name`, `status`,
  `label`, `reason`, and `url` while the link is fresh). It's separate from
  `bank`, which is still the bank name from the IBAN.
- **The callback finds its payment** by `rail_nonce`, a unique column holding
  the one-time `state`.
- **The reference line** fits the bank's `remittance_information_lines[0].max_length`.
  The reference always comes first and is never cut unless it's too long on its
  own; the description is added only if it fits whole.
- **The card keeps "I paid" for bank links**, because some banks never report
  a final status.
- **Checked against the real API:** a token signed for a made-up application
  id got Enable Banking's "Application does not exist". So the token format is
  read and the application looked up; the deck reports the error cleanly.
- **Redirect URL:** `FD_ENABLEBANKING_REDIRECT_URL` overrides the one the deck
  works out (`FD_PUBLIC_URL` or the browser's origin, plus
  `/fd/payments/bank/callback`). It has to match one registered on the
  application.

## Goal

The original ask: "user gets a link where to finish the payment". Captain sends
the prepared payment to the user's own bank through a licensed PSD2 provider
and hands the user one link. The link opens their bank (m-zaba, PBZ, Erste,
RBA, Revolut personal…). They approve the payment there with the bank's own
strong customer authentication (SCA), and Captain sees the bank's status.

Unlike Phase 1, this works for **personal accounts at most EU banks**, and
Captain never holds a bank login or a long-lived token. Each payment is its own
one-time authorisation at the bank.

## The provider: Enable Banking

| Why | Detail |
|---|---|
| Self-serve sandbox with payments enabled | "payment initiation is automatically enabled for all newly registered applications" (sandbox) |
| Croatian banks | Zagrebačka banka (SCA in m-zaba), PBZ, Erste, RBA and more; SEPA credit transfer and instant on all. **OTP banka is not yet supported** |
| Simple auth | An RS256 JWT per call, signed with the application's key: header `kid` = application id; claims `iss` = `enablebanking.com`, `aud` = `api.enablebanking.com`, `iat`/`exp` (at most 24 h). One base URL for both environments: `https://api.enablebanking.com` |

**Production needs a contract.** Enable Banking (like Yapily, TrueLayer and
every other provider) allows live payment initiation only for a company with
its own PISP licence, or under a TPP-infrastructure contract that uses the
provider's licence. A free "restricted production" mode exists for your own
accounts, but it covers account information, not payments. So Phase 2 is built
and tested on the sandbox; going live is a business step (contract, KYB),
not a code step.

## API used (verified against enablebanking.com/docs/api/reference, 2026-10-19)

- **`GET /aspsps?country=HR&service=PIS&psu_type=personal`**
  - Returns `{aspsps: [{name, country, logo, bic, psu_types, beta, payments: [...]}]}`.
  - Each `payments` entry describes one payment type: `payment_type`, `currencies`,
    `remittance_information_lines[{max_length}]`, `creditor_name_required`,
    `creditor_postal_address_required`, `final_successful_statuses`, …
- **`POST /payments`**
  - Body:
    `{payment_type: "SEPA", psu_type, aspsp: {name, country}, state, redirect_url, language, payment_request: {credit_transfer_transaction: [{instructed_amount: {amount: "84.20", currency: "EUR"}, beneficiary: {creditor: {name}, creditor_account: {scheme_name: "IBAN", identification}}, remittance_information: [lines], reference_number}]}}`.
  - Returns `{payment_id, status, url}`. `url` is the link the user opens.
- **The redirect back** goes to `redirect_url?state=…`, with `&error=…&error_description=…`
  on failure. No `error` means the user authorised the payment.
- **`GET /payments/{id}`** returns
  `{status, final_status, status_reason_information: {status_reason_code, status_reason_description}}`.
- **Statuses (ISO 20022):**
  - Received / pending: RCVD, PDNG.
  - Accepted: ACTC, ACCP, ACSP, ACWC, ACWP, ACPT, ACCR, ACSC, ACCC.
  - Rejected: RJCT.
  - Cancellation: CNCL, PACR, PDCR, RJCR, NULL.
- **References:** Croatian models have no reference-number scheme. `HR01 …`
  goes as the first `remittance_information` line, the same as in Phase 1. An
  RF reference goes in `reference_number`.

## How it works

1. **Deck setup (admin, once).**
   - Register an Enable Banking application (sandbox or production) and add
     `<deck>/fd/payments/bank/callback` to its allowed redirect URLs.
   - Set `FD_ENABLEBANKING_APP_ID` and `FD_ENABLEBANKING_KEY` (path to the
     private key PEM).
   - Flight Deck reports the environment and the redirect URL on the Payments
     page.
2. **User setup (once).** On the Payments page, pick your bank from Enable
   Banking's list (country, bank, personal or business).
3. **Handoff `rail=bank`** (agent) or "Pay in my bank" (card).
   - The same gate as every rail.
   - Flight Deck creates the payment at Enable Banking with a one-time `state`.
   - It records `rails_state.bank = {payment_id, state, aspsp, status, url, created_at}`.
   - The payment becomes `handed_off` (rail `bank`).
   - The agent gets the link to give the user. The phone page `/p/<token>` and
     the card show a "Pay in your bank" button while the link is fresh (1 hour).
4. **The user approves** in their bank (SCA), and the bank redirects to the callback.
   - The callback finds the payment by its `state`, reads the status, and shows
     a short "approved at your bank" or "not approved" page.
5. **Status sync** happens on every read (card, list, agent `get`/`list`), and
   on the callback:

| Enable Banking status | Ledger |
|---|---|
| RCVD, PDNG, or an accepted status not yet final | stays `handed_off` (the card says what the bank reports) |
| ACSC, ACCC; or `final_status` with an accepted status | **`settled`** (`paid_via=bank`) |
| RJCT | **`failed`** with the bank's reason |
| CNCL, RJCR, PACR, PDCR, or the redirect says `error` (user cancelled) | `cancelled` if final; otherwise the payment stays `handed_off` and can be handed over again |

Handing over again makes a new bank link, because an unapproved one expires.
The old one is simply left to expire; Enable Banking can delete only finished
payments.

## Out of scope for Phase 2

- A contract and production go-live
- Account information (reading the statement to confirm barcode payments): the
  same provider can do it, but that's the next step
- Standing orders
- Bulk payments
- Webhooks (the status is read on demand)
