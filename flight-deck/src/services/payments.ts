// REST client for payments (/fd/payments) — bill pay by barcode.
// Captain never moves money: an agent prepares a payment, the user scans its
// barcode in their bank app and approves it there. Only the owner can clear a
// blocking warning (ack) — never the agent.

import { useEffect, useState } from 'react'
import { useAuthStore, refreshAccessToken } from '../stores/authStore'

const FD_BASE = '/fd'

export type PaymentStatus =
  | 'draft' | 'needs_review' | 'ready' | 'handed_off'
  | 'user_paid' | 'settled' | 'skipped' | 'cancelled' | 'expired'

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
}

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
  fdJson<{ intent: Payment }>(`/payments/${encodeURIComponent(id)}`)

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
export const CLOSED: PaymentStatus[] = ['user_paid', 'settled', 'skipped', 'cancelled', 'expired']

export function statusLabel(s: PaymentStatus): string {
  return {
    draft: 'Not ready', needs_review: 'Needs your review', ready: 'Ready', handed_off: 'Ready to pay',
    user_paid: 'Paid', settled: 'Paid (bank-confirmed)', skipped: 'Skipped', cancelled: 'Cancelled',
    expired: 'Expired',
  }[s] ?? s
}
