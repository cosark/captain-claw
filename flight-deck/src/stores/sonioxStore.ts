import { create } from 'zustand'
import { useAuthStore, refreshAccessToken, registerSignOutTeardown } from './authStore'

// Soniox (speech-to-text and voice) — the user's own API key, through
// /fd/soniox/* (captain_claw/flight_deck/soniox_routes.py). Flight Deck keeps
// the key: it never comes back here, only its last four characters. Without a
// key of their own a user uses the team key an admin shared, else the
// server's SONIOX_API_KEY. On a deck with sign-in off the key is the deck's.

export interface SonioxStatus {
  key_set: boolean
  key_hint: string
  verified: boolean | null
  verify_error: 'refused' | 'unreachable' | null
  verified_at: string | null
  updated_at: string | null
  models: string[]
  // Whose key is in use: the user's own, the admin's team key, or the
  // server's SONIOX_API_KEY environment variable.
  source: 'own' | 'shared' | 'env' | null
  shared_available: boolean
  auth_enabled: boolean
  scope: 'user' | 'deck'
}

export interface SonioxTestResult {
  ok: boolean
  models: string[]
  latency_ms: number
}

interface SonioxStore {
  status: SonioxStatus | null
  loading: boolean
  error: string | null

  refresh: () => Promise<void>
  saveKey: (apiKey: string) => Promise<{ ok: boolean; warning?: string }>
  removeKey: () => Promise<boolean>
  test: () => Promise<SonioxTestResult | null>
  // Sign-in off only: move the server's SONIOX_API_KEY into Flight Deck.
  importEnv: () => Promise<{ ok: boolean; warning?: string }>
}

const _OLD_DECK = "This Flight Deck doesn't manage Soniox keys yet — it needs an update and a restart."

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const doFetch = () => {
    const { token } = useAuthStore.getState()
    return fetch(url, {
      credentials: 'include',
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init?.headers || {}),
      },
    })
  }
  let resp = await doFetch()
  if (resp.status === 401 && useAuthStore.getState().authEnabled && (await refreshAccessToken())) {
    resp = await doFetch()
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    const detail = typeof body?.detail === 'string' ? body.detail : ''
    if (resp.status === 405 || (resp.status === 404 && (!detail || detail === 'Not Found' || detail.startsWith('No route for')))) {
      throw new Error(_OLD_DECK)
    }
    if (resp.status === 401) throw new Error('Your Flight Deck session has expired — sign in again.')
    throw new Error(detail || `Flight Deck couldn't complete the Soniox request (HTTP ${resp.status}).`)
  }
  return (await resp.json()) as T
}

const errText = (exc: unknown) =>
  exc instanceof TypeError ? "Couldn't reach Flight Deck — check the connection and try again."
    : exc instanceof Error ? exc.message : String(exc)

type WithWarning = SonioxStatus & { warning?: string }

function _statusOnly(data: WithWarning): SonioxStatus {
  const { warning, ...status } = data
  void warning
  return status
}

export const useSonioxStore = create<SonioxStore>((set) => ({
  status: null,
  loading: false,
  error: null,

  // Flight Deck's record only — never a call to Soniox itself.
  refresh: async () => {
    set({ loading: true, error: null })
    try {
      set({ status: await fetchJson<SonioxStatus>('/fd/soniox/status'), loading: false })
    } catch (exc) {
      set({ loading: false, error: errText(exc) })
    }
  },

  // Flight Deck checks the key with Soniox before keeping it: a refused key
  // isn't saved (error); one it couldn't check is saved unverified (warning).
  saveKey: async (apiKey) => {
    set({ error: null })
    try {
      const data = await fetchJson<WithWarning>('/fd/soniox/key', {
        method: 'PUT',
        body: JSON.stringify({ api_key: apiKey }),
      })
      set({ status: _statusOnly(data) })
      return { ok: true, ...(data.warning ? { warning: data.warning } : {}) }
    } catch (exc) {
      set({ error: errText(exc) })
      return { ok: false }
    }
  },

  removeKey: async () => {
    set({ error: null })
    try {
      set({ status: await fetchJson<SonioxStatus>('/fd/soniox/key', { method: 'DELETE' }) })
      return true
    } catch (exc) {
      set({ error: errText(exc) })
      return false
    }
  },

  test: async () => {
    set({ error: null })
    let result: SonioxTestResult | null = null
    try {
      result = await fetchJson<SonioxTestResult>('/fd/soniox/test', { method: 'POST', body: '{}' })
    } catch (exc) {
      set({ error: errText(exc) })
    }
    // The test updates the key's record (verified, models) either way.
    try {
      set({ status: await fetchJson<SonioxStatus>('/fd/soniox/status') })
    } catch { /* keep the previous status */ }
    return result
  },

  importEnv: async () => {
    set({ error: null })
    try {
      const data = await fetchJson<WithWarning>('/fd/soniox/import-env', { method: 'POST', body: '{}' })
      set({ status: _statusOnly(data) })
      return { ok: true, ...(data.warning ? { warning: data.warning } : {}) }
    } catch (exc) {
      set({ error: errText(exc) })
      return { ok: false }
    }
  },
}))

// The next person at a shared screen must not see the outgoing user's key state.
registerSignOutTeardown(() => {
  useSonioxStore.setState({ status: null, loading: false, error: null })
})
