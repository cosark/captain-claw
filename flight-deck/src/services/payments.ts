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
  fdJson<{ intent: Payment; rails: string[] }>(`/payments/${encodeURIComponent(id)}`)

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
