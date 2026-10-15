// REST client for what an admin sets up once for the whole deck:
// - the shared Telegram bot (/fd/telegram-shared for a user's own link,
//   /fd/admin/telegram-shared for the token and the access requests) —
//   captain_claw/flight_deck/telegram_shared_routes.py
// - the team Jev key (/fd/jev/shared, /fd/jev/shared-key) — jev_routes.py
//
// None of these ever returns a token or key, only a hint of its last characters.

import { useAuthStore, refreshAccessToken } from '../stores/authStore'

function _authHeaders(): Record<string, string> {
  const { token, authEnabled } = useAuthStore.getState()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authEnabled && token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

async function fdFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const _state = useAuthStore.getState()
  if (_state.authEnabled === true && !_state.token) {
    const refreshed = await refreshAccessToken()
    if (!refreshed) throw new Error('Not authenticated')
  }
  const build = (): RequestInit => ({ headers: _authHeaders(), credentials: 'include', ...init })
  let res = await fetch(path, build())
  // Only a failed refresh ends the session (see docker.ts fdFetch).
  if (res.status === 401 && useAuthStore.getState().authEnabled) {
    if (!(await refreshAccessToken())) {
      useAuthStore.getState().clearAuth()
      throw new Error('Session expired')
    }
    res = await fetch(path, build())
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    let msg = body.detail
    if (Array.isArray(msg)) {
      msg = msg.map((e: { loc?: string[]; msg?: string }) => `${(e.loc || []).join('.')}: ${e.msg}`).join('; ')
    }
    throw new Error(typeof msg === 'string' && msg ? msg : `HTTP ${res.status}`)
  }
  return res.json()
}

// sessionStorage key: the Admin tab to open next (e.g. 'integrations').
export const ADMIN_TAB_KEY = 'fd:admin-tab'

// ── Shared Telegram bot ──

export type TelegramLinkStatus = 'pending' | 'approved' | 'rejected'

export interface TelegramLink {
  status: TelegramLinkStatus
  agent_slug: string
  agent_name: string
  // A Telegram account has opened the bot through the one-time link.
  linked: boolean
  tg_username: string
  tg_first_name: string
  requested_at: string | null
  decided_at: string | null
  linked_at: string | null
  // t.me/<bot>?start=<code> until a Telegram account is linked.
  deep_link?: string
  // Is the agent polling Flight Deck's relay (null: not seen since FD started).
  agent_online: boolean | null
}

export interface OwnAgent {
  slug: string
  name: string
  kind: string
  status: string
}

export interface MyTelegram {
  auth_enabled: boolean
  available: boolean
  bot_username: string
  link: TelegramLink | null
  agents: OwnAgent[]
}

export const getMyTelegram = () => fdFetch<MyTelegram>('/fd/telegram-shared')

export const requestTelegram = (agent_slug: string) =>
  fdFetch<MyTelegram>('/fd/telegram-shared/request', {
    method: 'POST',
    body: JSON.stringify({ agent_slug }),
  })

export const disconnectTelegram = () =>
  fdFetch<MyTelegram>('/fd/telegram-shared', { method: 'DELETE' })

export interface AdminTelegramLink extends TelegramLink {
  user_id: string
  user_email: string
  user_name: string
}

export interface AdminTelegram {
  auth_enabled: boolean
  config: {
    configured: boolean
    enabled: boolean
    bot_username: string
    bot_name: string
    token_hint: string
  }
  status: { running: boolean; last_ok_at: string | null; error: string }
  links: AdminTelegramLink[]
}

export const getAdminTelegram = () => fdFetch<AdminTelegram>('/fd/admin/telegram-shared')

export const saveAdminTelegram = (patch: { token?: string; enabled?: boolean }) =>
  fdFetch<AdminTelegram>('/fd/admin/telegram-shared', {
    method: 'PUT',
    body: JSON.stringify(patch),
  })

export const clearAdminTelegram = () =>
  fdFetch<AdminTelegram>('/fd/admin/telegram-shared', { method: 'DELETE' })

export const decideTelegramLink = (userId: string, decision: 'approve' | 'reject') =>
  fdFetch<AdminTelegram>(
    `/fd/admin/telegram-shared/links/${encodeURIComponent(userId)}/${decision}`,
    { method: 'POST' },
  )

export const removeTelegramLink = (userId: string) =>
  fdFetch<AdminTelegram>(`/fd/admin/telegram-shared/links/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
  })

// ── Team Jev key ──

export interface SharedJevKey {
  key_set: boolean
  key_hint: string
  verified: boolean | null
  verify_error: 'refused' | 'unreachable' | null
  verified_at: string | null
  updated_at: string | null
  models: string[]
  available: boolean
  warning?: string
}

export const getSharedJevKey = () => fdFetch<SharedJevKey>('/fd/jev/shared')

export const saveSharedJevKey = (api_key: string) =>
  fdFetch<SharedJevKey>('/fd/jev/shared-key', {
    method: 'PUT',
    body: JSON.stringify({ api_key }),
  })

export const removeSharedJevKey = () =>
  fdFetch<SharedJevKey>('/fd/jev/shared-key', { method: 'DELETE' })

// ── Google / Microsoft OAuth client (already one per deck, admin-managed) ──

export interface OAuthClientConfig {
  client_id: string
  client_id_set: boolean
  client_secret_set: boolean
}

export const getOAuthClientConfig = (provider: 'google' | 'microsoft') =>
  fdFetch<OAuthClientConfig>(`/fd/${provider}/config`)
