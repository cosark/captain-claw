// REST client for payments (/fd/payments) — bill pay by barcode.
// Captain never moves money: an agent prepares a payment, the user scans its
// barcode in their bank app and approves it there. Only the owner can clear a
// blocking warning (ack) — never the agent.

import { useEffect, useState } from 'react'
import { useAuthStore, refreshAccessToken } from '../stores/authStore'

const FD_BASE = '/fd'

export type PaymentStatus =
  | 'draft' | 'needs_review' | 'ready' | 'handed_off'
  | 'user_paid' | 'settled' | 'skipped' | 'cancelled' | 'expired' | 'failed'

export interface PaymentWarning {
  code: string
  severity: 'error' | 'blocking' | 'info'
  message: string
  acked: boolean
}

export interface Payment {
  id: string
  status: PaymentStatus
  payee_name: string
  payee_street: string
  payee_city: string
  payee_oib: string
  iban: string
  iban_grouped: string
  bank: string
  amount_cents: number
  amount_text: string
  currency: string
  model: string
  reference: string
  purpose_code: string
  description: string
  due_date: string
  /** field → 'barcode' | 'document' | 'model' | 'user' */
  provenance: Record<string, string>
  warnings: PaymentWarning[]
  barcodes: ('hub3' | 'epc')[]
  source_name: string
  source_kind: string
  automated: boolean
  supersedes: string
  created_at: number
  updated_at: number
  paid_at: number | null
  paid_via: string
  page_active: boolean
  handoff_rail: string
  /** Revolut Business draft (Phase 1): its id, last state, reason, and the VoP check. */
  revolut: { draft_id?: string; state?: string; reason?: string; vop?: { result_code: string; name: string } }
  /** Pay-by-bank link (Phase 2): the bank, its status in words, and the link while it's fresh. */
  bank_link: { bank_name?: string; status?: string; label?: string; reason?: string; url?: string }
  /** What the bank statement says (Phase 4b): booked on which account, pending, or payments to confirm. */
  statement?: {
    booked_on: string; account_label: string; match: string
    pending_since: string; pending_account: string; suggestions: number
  }
}

/** A payment on the statement that may be this bill — the owner says yes or no. */
export interface StatementSuggestion {
  fp: string; day: string; amount_cents: number; creditor_name: string; account_label: string; reason: string
}

export interface BankLinkStatus {
  configured: boolean
  environment: string
  redirect_url: string
  bank: { name: string; country: string; psu_type: string } | null
  error?: string
}

export interface BankOption { name: string; country: string; logo: string; beta: boolean; bic: string }

export interface RevolutConnection {
  connected: boolean
  environment: 'production' | 'sandbox'
  client_id: string
  redirect_uri: string
  has_key: boolean
  certificate: string
  account_id: string
  account_label: string
  connected_at: number | null
  scope: string
  /** Shopping: a Revolut card per purchase (Phase 3). */
  cards?: boolean
}

export interface RevolutAccount { id: string; name: string; currency: string; balance: number; state: string }

export interface PaymentsConfig {
  enabled: boolean
  deps_ok: boolean
  cap_cents: number
  default_cap_cents: number
}

function _authHeaders(): Record<string, string> {
  const { token, authEnabled } = useAuthStore.getState()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authEnabled && token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

async function _fetch(path: string, init?: RequestInit): Promise<Response> {
  const _state = useAuthStore.getState()
  if (_state.authEnabled === true && !_state.token) {
    if (!(await refreshAccessToken())) throw new Error('Not authenticated')
  }
  let res = await fetch(`${FD_BASE}${path}`, { headers: _authHeaders(), credentials: 'include', ...init })
  if (res.status === 401 && useAuthStore.getState().authEnabled) {
    if (!(await refreshAccessToken())) {
      useAuthStore.getState().clearAuth()
      throw new Error('Session expired')
    }
    res = await fetch(`${FD_BASE}${path}`, { headers: _authHeaders(), credentials: 'include', ...init })
  }
  return res
}

async function fdJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await _fetch(path, init)
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(body.detail || `${res.status}`)
  }
  return res.json()
}

export const getPaymentsConfig = () => fdJson<PaymentsConfig>('/payments/config')

export const listPayments = (status = '') =>
  fdJson<{ payments: Payment[] } & PaymentsConfig>(`/payments${status ? `?status=${encodeURIComponent(status)}` : ''}`)

export const getPayment = (id: string) =>
  fdJson<{ intent: Payment; rails: string[]; statement_suggestions?: StatementSuggestion[] }>(`/payments/${encodeURIComponent(id)}`)
export const answerStatementMatch = (id: string, fp: string, accept: boolean) =>
  fdJson<{ intent: Payment; statement_suggestions: StatementSuggestion[] }>(
    `/payments/${encodeURIComponent(id)}/statement-match`, { method: 'POST', body: JSON.stringify({ fp, accept }) })

export const sendToRevolut = (id: string) =>
  fdJson<{ intent: Payment }>(`/payments/${encodeURIComponent(id)}/revolut`, { method: 'POST' })

// Pay-by-bank links (PSD2 via the deck's Enable Banking application).
export const getBankLink = () => fdJson<BankLinkStatus>('/payments/bank/status')
export const bankOptions = (country: string, psu_type: string) =>
  fdJson<{ banks: BankOption[] }>(`/payments/bank/aspsps?country=${encodeURIComponent(country)}&psu_type=${psu_type}`)
export const setBankChoice = (name: string, country: string, psu_type: string) =>
  fdJson<{ bank: BankLinkStatus['bank'] }>('/payments/bank/choice', {
    method: 'PUT', body: JSON.stringify({ name, country, psu_type }),
  })
export const clearBankChoice = () => fdJson<{ bank: null }>('/payments/bank/choice', { method: 'DELETE' })
export const payByBank = (id: string) =>
  fdJson<{ intent: Payment; url: string; bank_name: string }>(`/payments/${encodeURIComponent(id)}/bank`, { method: 'POST' })

// Revolut Business connection (scope READ,WRITE: drafts only, never payments).
export const getRevolut = () => fdJson<RevolutConnection>('/payments/revolut/status')
export const makeRevolutKey = (environment: string) =>
  fdJson<RevolutConnection>('/payments/revolut/key', { method: 'POST', body: JSON.stringify({ environment }) })
export const setRevolutClient = (client_id: string, environment: string) =>
  fdJson<RevolutConnection>('/payments/revolut/client', {
    method: 'PUT', body: JSON.stringify({ client_id, environment }),
  })
export const revolutConnectUrl = () => fdJson<{ url: string }>('/payments/revolut/connect', { method: 'POST' })
export const pasteRevolutCode = (code: string) =>
  fdJson<RevolutConnection>('/payments/revolut/code', { method: 'POST', body: JSON.stringify({ code }) })
export const revolutAccounts = () => fdJson<{ accounts: RevolutAccount[] }>('/payments/revolut/accounts')
export const setRevolutAccount = (account_id: string) =>
  fdJson<RevolutConnection>('/payments/revolut/account', { method: 'PUT', body: JSON.stringify({ account_id }) })
export const disconnectRevolut = () =>
  fdJson<RevolutConnection>('/payments/revolut/connection', { method: 'DELETE' })

export const ackWarnings = (id: string, codes: string[]) =>
  fdJson<{ intent: Payment }>(`/payments/${encodeURIComponent(id)}/ack`, {
    method: 'POST', body: JSON.stringify({ codes }),
  })

export const markPayment = (id: string, status: 'user_paid' | 'skipped' | 'cancelled') =>
  fdJson<{ intent: Payment }>(`/payments/${encodeURIComponent(id)}/mark`, {
    method: 'POST', body: JSON.stringify({ status }),
  })

export const setPaymentsCap = (cap_cents: number) =>
  fdJson<PaymentsConfig>('/payments/settings', { method: 'PUT', body: JSON.stringify({ cap_cents }) })

/** Whether this deck has payments on (the sidebar shows the page only then). */
export function usePaymentsEnabled(): boolean {
  const signedIn = useAuthStore((s) => s.isAuthenticated || s.authEnabled === false)
  const [on, setOn] = useState(false)
  useEffect(() => {
    if (!signedIn) return
    let alive = true
    getPaymentsConfig().then((c) => { if (alive) setOn(c.enabled) }).catch(() => { if (alive) setOn(false) })
    return () => { alive = false }
  }, [signedIn])
  return on
}

/** The barcode PNG as an object URL (fetched with the session's auth). */
export function useBarcode(id: string, kind: string, active: boolean): string | null {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!active) { setUrl(null); return }
    let revoked = false
    let made: string | null = null
    _fetch(`/payments/${encodeURIComponent(id)}/barcode/${kind}.png`)
      .then((res) => (res.ok ? res.blob() : null))
      .then((blob) => {
        if (!blob || revoked) return
        made = URL.createObjectURL(blob)
        setUrl(made)
      })
      .catch(() => setUrl(null))
    return () => {
      revoked = true
      if (made) URL.revokeObjectURL(made)
    }
  }, [id, kind, active])
  return url
}

/** A payment id an agent's `payments` tool row carries ("payment_intent: pay_… status: …"). */
const _INTENT_RE = /^payment_intent: (pay_[0-9a-f]{12}) status: (\w+)/
export function paymentIntentOf(toolName: string | undefined, output: string | undefined): string | null {
  if (toolName !== 'payments' || !output) return null
  const m = _INTENT_RE.exec(output.trimStart())
  return m ? m[1] : null
}

export const PAID: PaymentStatus[] = ['user_paid', 'settled']
export const CLOSED: PaymentStatus[] = ['user_paid', 'settled', 'skipped', 'cancelled', 'expired', 'failed']

export function statusLabel(s: PaymentStatus): string {
  return {
    draft: 'Not ready', needs_review: 'Needs your review', ready: 'Ready', handed_off: 'Ready to pay',
    user_paid: 'Paid', settled: 'Paid (bank-confirmed)', skipped: 'Skipped', cancelled: 'Cancelled',
    expired: 'Expired', failed: 'Not completed',
  }[s] ?? s
}

// ── Bank statements (Phase 4): read-only, outgoing payments only ──

export interface StatementAccount {
  uid: string
  label: string
  currency: string
  watched: boolean
  synced_through: string
  outgoing: number
}
export interface StatementSession {
  provider: 'enablebanking' | 'revolut'
  session_id: string
  bank: string
  country: string
  status: 'active' | 'needs_renewal' | 'revoked'
  valid_until: number | null
  last_sync_at: number | null
  next_sync_at: number | null
  last_error: string
  unattended_left: number | null
  accounts: StatementAccount[]
}
export interface StatementsStatus {
  enabled: boolean
  configured: boolean
  revolut_connected: boolean
  not_on_statement: boolean
  sync_times: string[]
  redirect_url: string
  sessions: StatementSession[]
}
export interface StatementBank { name: string; country: string; logo: string; beta: boolean; consent_days: number }

export const getStatements = () => fdJson<StatementsStatus>('/payments/statements')
export const statementBanks = (country: string, psu_type: string) =>
  fdJson<{ banks: StatementBank[] }>(`/payments/statements/aspsps?country=${encodeURIComponent(country)}&psu_type=${psu_type}`)
export const connectStatements = (name: string, country: string, psu_type: string) =>
  fdJson<{ url: string }>('/payments/statements/connect', {
    method: 'POST', body: JSON.stringify({ name, country, psu_type }),
  })
export const syncStatements = () =>
  fdJson<{ results: { provider: string; new: number; error: string }[]; sessions: StatementSession[] }>(
    '/payments/statements/sync', { method: 'POST' })
export const watchStatementAccount = (uid: string, watched: boolean) =>
  fdJson<{ ok: true }>(`/payments/statements/accounts/${encodeURIComponent(uid)}`, {
    method: 'PUT', body: JSON.stringify({ watched }),
  })
export const disconnectStatements = (provider: string, sessionId: string) =>
  fdJson<{ ok: true }>(`/payments/statements/${provider}/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
export const setStatementWarnings = (not_on_statement: boolean) =>
  fdJson<{ not_on_statement: boolean }>('/payments/statements/settings', {
    method: 'PUT', body: JSON.stringify({ not_on_statement }),
  })
export const setRevolutStatement = (enabled: boolean) =>
  fdJson<{ enabled: boolean; message?: string }>('/payments/statements/revolut', {
    method: 'PUT', body: JSON.stringify({ enabled }),
  })

// ── Purchases (Phase 3): Captain buys from a shop with a one-time card ──

export type PurchaseStatus =
  | 'awaiting_approval' | 'approved' | 'paying' | 'needs_action'
  | 'ordered' | 'completed' | 'failed' | 'denied' | 'expired' | 'cancelled'

export interface Purchase {
  id: string
  status: PurchaseStatus
  source: 'link' | 'revolut_card' | 'acp' | 'ap2'
  merchant_name: string
  merchant_url: string
  merchant_domain: string
  description: string
  items: { name: string; quantity?: number; unit_amount?: string; url?: string }[]
  amount_cents: number
  currency: string
  amount_text: string
  approval_url: string
  card_last4: string
  order_ref: string
  result: { outcome?: string; title?: string; url?: string; excerpt?: string }
  warnings: { code: string; severity: string; message: string }[]
  automated: boolean
  next_action: { display_message?: string; action_url?: string | null; type?: string }
  test: boolean
  created_at: number
  completed_at: number | null
}

export interface LinkStatus {
  configured: boolean
  connected: boolean
  test_mode: boolean
  redirect_uri: string
  user: { email: string; name: string } | null
  error?: string
}

export const listPurchases = () => fdJson<{ purchases: Purchase[]; sources: string[] }>('/payments/purchases')
export const getPurchase = (id: string) => fdJson<{ purchase: Purchase }>(`/payments/purchases/${encodeURIComponent(id)}`)
export const approvePurchase = (id: string) =>
  fdJson<{ purchase: Purchase }>(`/payments/purchases/${encodeURIComponent(id)}/approve`, { method: 'POST' })
export const denyPurchase = (id: string) =>
  fdJson<{ purchase: Purchase }>(`/payments/purchases/${encodeURIComponent(id)}/deny`, { method: 'POST' })
export const getLink = () => fdJson<LinkStatus>('/payments/link/status')
export const linkConnectUrl = () => fdJson<{ url: string }>('/payments/link/connect', { method: 'POST' })
export const disconnectLink = () => fdJson<{ connected: false }>('/payments/link/connection', { method: 'DELETE' })
export const setRevolutCards = (enabled: boolean) =>
  fdJson<{ enabled: boolean; url?: string }>('/payments/revolut/cards', { method: 'POST', body: JSON.stringify({ enabled }) })
export const answerChallenge = (id: string, response: string) =>
  fdJson<{ purchase: Purchase }>(`/payments/purchases/${encodeURIComponent(id)}/challenge`, {
    method: 'POST', body: JSON.stringify({ response }),
  })

// Checkout protocols: ACP shops (their key per user) and AP2 (your credentials provider + signing key).
export interface AcpMerchant { domain: string; name: string; added_at: number | null }
export interface Ap2Settings {
  enabled: boolean
  credentials_provider_url: string
  user_email: string
  public_key: string
  kid: string
  shopping_agent_id: string
}
export const listAcpMerchants = () => fdJson<{ merchants: AcpMerchant[] }>('/payments/acp/merchants')
export const addAcpMerchant = (domain: string, api_key: string, name: string) =>
  fdJson<{ merchants: AcpMerchant[] }>('/payments/acp/merchants', {
    method: 'PUT', body: JSON.stringify({ domain, api_key, name }),
  })
export const removeAcpMerchant = (domain: string) =>
  fdJson<{ merchants: AcpMerchant[] }>(`/payments/acp/merchants/${encodeURIComponent(domain)}`, { method: 'DELETE' })
export const getAp2Settings = () => fdJson<Ap2Settings>('/payments/ap2/settings')
export const setAp2Settings = (credentials_provider_url: string, user_email: string) =>
  fdJson<Ap2Settings>('/payments/ap2/settings', {
    method: 'PUT', body: JSON.stringify({ credentials_provider_url, user_email }),
  })

/** A purchase id a `payments` tool row carries ("purchase_intent: pur_… status: …"). */
const _PURCHASE_RE = /purchase_intent: (pur_[0-9a-f]{12}) status: (\w+)/
export function purchaseIntentOf(toolName: string | undefined, output: string | undefined): string | null {
  if (toolName !== 'payments' || !output) return null
  const m = _PURCHASE_RE.exec(output)
  return m ? m[1] : null
}

export const PURCHASE_LABEL: Record<PurchaseStatus, string> = {
  awaiting_approval: 'Waiting for your approval', approved: 'Approved — Captain can pay',
  paying: 'Paying…', needs_action: 'Your bank wants you to confirm', ordered: 'Ordered',
  completed: 'Ordered and paid', failed: 'Not completed', denied: 'Denied', expired: 'Expired', cancelled: 'Cancelled',
}
