import { create } from 'zustand'
import { useAuthStore, refreshAccessToken, registerSignOutTeardown } from './authStore'

// Jev (TypeSafe AI) — the user's own API key and the Connections playground,
// through /fd/jev/* (captain_claw/flight_deck/jev_routes.py). Flight Deck keeps
// the key: it never comes back here, only its last four characters (key_hint).
// On a deck with sign-in off the one key belongs to the deck (scope 'deck').

export type JevQuestionType = 'noul' | 'choice' | 'score'

export interface JevStatus {
  key_set: boolean
  key_hint: string
  verified: boolean | null
  // Why an unverified key is unverified: Jev refused it (replace it), or Jev
  // couldn't be reached to check it.
  verify_error: 'refused' | 'unreachable' | null
  verified_at: string | null
  updated_at: string | null
  models: string[]
  default_model: string
  auth_enabled: boolean
  scope: 'user' | 'deck'
}

export interface JevModel {
  name: string
  description?: string
  release_date?: string
}

export interface JevTestResult {
  ok: boolean
  models: JevModel[]
  latency_ms: number
}

// A question as the API takes it (jev_client.normalize_questions): a noul's
// criteria say what yes / no mean, a choice's map option → description (or
// null), a score's are its levels, lowest first.
export interface JevQuestion {
  type: JevQuestionType
  instructions: string
  criteria?: Record<string, string | null> | string[]
}

export interface JevAskBody {
  // Text; a string holding a JSON object / array is sent as structured state.
  state: string
  questions: Record<string, JevQuestion>
  model?: string
}

export interface JevNoulAnswer {
  type: 'noul'
  noul: number
}

export interface JevChoiceAnswer {
  type: 'choice'
  choice: string
  probabilities: Record<string, number>
  confidence: number
}

export interface JevScoreAnswer {
  type: 'score'
  score: number
  // Level number ("0", "1", …) → its description, as given.
  legend: Record<string, unknown>
  probabilities: Record<string, number>
  confidence: number
}

export type JevAnswer = JevNoulAnswer | JevChoiceAnswer | JevScoreAnswer

export interface JevAskResponse {
  model: string
  answers: Record<string, JevAnswer>
  usage: { input_tokens?: number; output_tokens?: number }
  // Each answer's headline value: p(yes) / the chosen option / the expected level.
  values: Record<string, number | string | null>
  // The plain-text account an agent reads (jev_client.format_answers).
  summary: string
  latency_ms: number
}

interface JevStore {
  status: JevStatus | null
  loading: boolean
  error: string | null

  refresh: () => Promise<void>
  saveKey: (apiKey: string) => Promise<{ ok: boolean; warning?: string }>
  removeKey: () => Promise<boolean>
  test: () => Promise<JevTestResult | null>
  ask: (body: JevAskBody) => Promise<JevAskResponse>
}

function authHeaders(): Record<string, string> {
  const { token } = useAuthStore.getState()
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

function _detailText(detail: unknown): string {
  if (typeof detail === 'string') return detail
  // FastAPI body validation: [{loc, msg}, …]
  if (Array.isArray(detail)) {
    return detail
      .slice(0, 5)
      .map((item) => {
        if (!item || typeof item !== 'object') return String(item)
        const { loc, msg } = item as { loc?: unknown; msg?: unknown }
        const where = Array.isArray(loc) ? loc.filter((p) => p !== 'body').join('.') : ''
        return where ? `${where}: ${String(msg)}` : String(msg)
      })
      .join('; ')
  }
  return ''
}

export class HttpError extends Error {
  readonly status: number
  readonly detail: string
  // Seconds Jev asked to wait (429), when Flight Deck passed it on.
  readonly retryAfter: number | null

  constructor(status: number, statusText: string, text: string, retryAfter: string | null = null) {
    super(`${status} ${statusText}: ${text}`)
    this.status = status
    let detail = ''
    try {
      const parsed: unknown = JSON.parse(text)
      if (parsed && typeof parsed === 'object') detail = _detailText((parsed as { detail?: unknown }).detail)
    } catch { /* not JSON */ }
    this.detail = detail
    const wait = retryAfter === null ? NaN : Number(retryAfter)
    this.retryAfter = Number.isFinite(wait) && wait >= 0 ? wait : null
  }
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const doFetch = () => fetch(url, {
    credentials: 'include',
    ...init,
    headers: {
      ...authHeaders(),
      ...(init?.headers || {}),
    },
  })
  let resp = await doFetch()
  if (resp.status === 401 && useAuthStore.getState().authEnabled && (await refreshAccessToken())) {
    resp = await doFetch()
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    throw new HttpError(resp.status, resp.statusText, text, resp.headers.get('Retry-After'))
  }
  return (await resp.json()) as T
}

const _OLD_DECK = "This Flight Deck doesn't offer Jev yet — it needs an update and a restart."
const _SESSION_EXPIRED = 'Your Flight Deck session has expired — sign in again.'

/** The message to show for a failed /fd/jev call: the server's own detail
 *  where there is one. (Upstream Jev trouble never arrives as a 401 — that is
 *  always this browser's Flight Deck session.) */
export function jevErrorText(exc: unknown): string {
  if (exc instanceof HttpError) {
    // A deck without the Jev routes: the SPA catch-all's 404 for GET, a 405
    // for anything else.
    if (exc.status === 405) return _OLD_DECK
    if (exc.status === 404 && (!exc.detail || exc.detail === 'Not Found' || exc.detail.startsWith('No route for'))) {
      return _OLD_DECK
    }
    if (exc.status === 401) return _SESSION_EXPIRED
    if (exc.detail) {
      if (exc.status === 429 && exc.retryAfter !== null && !/retry|again/i.test(exc.detail)) {
        return `${exc.detail} — retry in ${Math.ceil(exc.retryAfter)} s`
      }
      return exc.detail
    }
    if (exc.status >= 500) return `Flight Deck couldn't complete the Jev request (HTTP ${exc.status}).`
  }
  if (exc instanceof TypeError) return "Couldn't reach Flight Deck — check the connection and try again."
  return exc instanceof Error ? exc.message : String(exc)
}

type SaveKeyResponse = JevStatus & { warning?: string }

function _statusOnly(data: SaveKeyResponse): JevStatus {
  const { warning, ...status } = data
  void warning
  return status
}

export const useJevStore = create<JevStore>((set, get) => ({
  status: null,
  loading: false,
  error: null,

  // Flight Deck's record only — never a call to Jev itself.
  refresh: async () => {
    set({ loading: true, error: null })
    try {
      const status = await fetchJson<JevStatus>('/fd/jev/status')
      set({ status, loading: false })
    } catch (exc) {
      set({ loading: false, error: jevErrorText(exc) })
    }
  },

  // Flight Deck checks the key with Jev before keeping it: a key Jev refuses
  // isn't saved (error); one it couldn't check is saved unverified (warning).
  saveKey: async (apiKey) => {
    set({ error: null })
    try {
      const data = await fetchJson<SaveKeyResponse>('/fd/jev/key', {
        method: 'PUT',
        body: JSON.stringify({ api_key: apiKey }),
      })
      set({ status: _statusOnly(data) })
      return { ok: true, ...(data.warning ? { warning: data.warning } : {}) }
    } catch (exc) {
      set({ error: jevErrorText(exc) })
      return { ok: false }
    }
  },

  removeKey: async () => {
    set({ error: null })
    try {
      const status = await fetchJson<JevStatus>('/fd/jev/key', { method: 'DELETE' })
      set({ status })
      return true
    } catch (exc) {
      set({ error: jevErrorText(exc) })
      return false
    }
  },

  test: async () => {
    set({ error: null })
    let result: JevTestResult | null = null
    try {
      result = await fetchJson<JevTestResult>('/fd/jev/test', { method: 'POST', body: '{}' })
    } catch (exc) {
      set({ error: jevErrorText(exc) })
    }
    // The test updates the key's record (verified, models) either way.
    try {
      set({ status: await fetchJson<JevStatus>('/fd/jev/status') })
    } catch { /* keep the previous status; the next refresh() reports it */ }
    return result
  },

  // Throws HttpError — the playground shows jevErrorText() of it. A run also
  // tells Flight Deck whether the key works (a success verifies it, a refusal
  // marks it refused, a 404 means it's gone), so the status is re-read quietly
  // — no spinner, no error banner — when that may have changed.
  ask: async (body) => {
    const reread = async () => {
      try {
        set({ status: await fetchJson<JevStatus>('/fd/jev/status') })
      } catch { /* keep the previous status */ }
    }
    try {
      const resp = await fetchJson<JevAskResponse>('/fd/jev/ask', { method: 'POST', body: JSON.stringify(body) })
      if (get().status?.verified !== true) await reread()
      return resp
    } catch (exc) {
      if (exc instanceof HttpError && (exc.status === 400 || exc.status === 404)) await reread()
      throw exc
    }
  },
}))

// The next person at a shared screen must not see the outgoing user's key
// state (the playground itself lives in the card and goes with the reload).
registerSignOutTeardown(() => {
  useJevStore.setState({ status: null, loading: false, error: null })
})
