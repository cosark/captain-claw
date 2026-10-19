// REST client for what an admin sets up once for the whole deck:
// - the shared Telegram bot (/fd/telegram-shared for a user's own link,
//   /fd/admin/telegram-shared for the token and the access requests) —
//   captain_claw/flight_deck/telegram_shared_routes.py
// - the Telegram inbox through that bot (/fd/telegram-inbox) —
//   captain_claw/flight_deck/telegram_inbox_routes.py
// - the team Jev key (/fd/jev/shared, /fd/jev/shared-key) — jev_routes.py
// - the team Soniox key (/fd/soniox/shared, /shared-key, /import-env) — soniox_routes.py
// - the deck's logo (/fd/admin/branding/logo) — branding_routes.py
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
  // The admin approves requests automatically (a turned-down one still waits).
  auto_approve?: boolean
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
    // New requests are approved at once (not someone an admin turned down).
    auto_approve: boolean
    // People may keep a Telegram inbox through the bot (read-only).
    inbox: boolean
    bot_username: string
    bot_name: string
    token_hint: string
  }
  status: { running: boolean; last_ok_at: string | null; error: string; bot_flags?: BotFlags }
  links: AdminTelegramLink[]
}

export const getAdminTelegram = () => fdFetch<AdminTelegram>('/fd/admin/telegram-shared')

export const saveAdminTelegram = (patch: { token?: string; enabled?: boolean; auto_approve?: boolean; inbox?: boolean }) =>
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

// What BotFather allows the bot (null: not checked yet): connecting to people's
// accounts through Chat Automation (Business Mode), reading every group message
// (privacy mode off).
export interface BotFlags {
  business: boolean | null
  groups: boolean | null
}

// ── Telegram inbox ──

export interface InboxGroup {
  chat_id: string
  kind: 'group'
  title: string
  active: boolean
  messages: number
  last_at: string | null
}

export interface TelegramInbox {
  available: boolean
  reason: string
  bot_username: string
  bot: BotFlags
  // The user's Telegram account is linked to the team bot and approved.
  linked: boolean
  tg_username: string
  enabled: boolean
  collecting: boolean
  consented_at: string | null
  retention_days: number
  retention_choices: number[]
  // A live Chat Automation connection, and the rights the user granted it (unused).
  connection: { connected_at: string | null; rights: string[] } | null
  groups: InboxGroup[]
  private_chats: number
  stats: { messages: number; chats: number; oldest: string | null; newest: string | null }
}

export interface InboxMessage {
  chat_id: string
  chat: string
  kind: string
  message_id: number
  at: string | null
  outgoing: boolean
  sender: string
  text: string
  media: string
  forwarded_from: string
  edited: boolean
  deleted: boolean
}

export const getTelegramInbox = () => fdFetch<TelegramInbox>('/fd/telegram-inbox')

export const setTelegramInbox = (patch: { enabled?: boolean; consent?: boolean; retention_days?: number }) =>
  fdFetch<TelegramInbox>('/fd/telegram-inbox', { method: 'PUT', body: JSON.stringify(patch) })

export const clearTelegramInbox = () =>
  fdFetch<TelegramInbox>('/fd/telegram-inbox/messages', { method: 'DELETE' })

export const removeInboxGroup = (chatId: string) =>
  fdFetch<TelegramInbox>(`/fd/telegram-inbox/groups/${encodeURIComponent(chatId)}`, { method: 'DELETE' })

export const getTelegramInboxRecent = (limit = 20) =>
  fdFetch<{ messages: InboxMessage[] }>(`/fd/telegram-inbox/recent?limit=${limit}`)

export const removeTelegramLink = (userId: string) =>
  fdFetch<AdminTelegram>(`/fd/admin/telegram-shared/links/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
  })

// ── Branding logo ──

/** Upload the deck's logo (a `data:` URL of the image file); resolves with its URL. */
export const uploadBrandingLogo = (dataUrl: string) =>
  fdFetch<{ ok: boolean; logo: string }>('/fd/admin/branding/logo', {
    method: 'PUT',
    body: JSON.stringify({ data_url: dataUrl }),
  })

export const deleteBrandingLogo = () =>
  fdFetch<{ ok: boolean; logo: string }>('/fd/admin/branding/logo', { method: 'DELETE' })

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

// ── Team Soniox key ──

export interface SharedSonioxKey {
  key_set: boolean
  key_hint: string
  verified: boolean | null
  verify_error: 'refused' | 'unreachable' | null
  verified_at: string | null
  updated_at: string | null
  models: string[]
  available: boolean
  // The server's environment still carries SONIOX_API_KEY (the old way).
  env_key_set: boolean
  env_key_hint: string
  warning?: string
}

export const getSharedSonioxKey = () => fdFetch<SharedSonioxKey>('/fd/soniox/shared')

export const saveSharedSonioxKey = (api_key: string) =>
  fdFetch<SharedSonioxKey>('/fd/soniox/shared-key', {
    method: 'PUT',
    body: JSON.stringify({ api_key }),
  })

export const removeSharedSonioxKey = () =>
  fdFetch<SharedSonioxKey>('/fd/soniox/shared-key', { method: 'DELETE' })

/** Move the server's SONIOX_API_KEY into Flight Deck as the team key. */
export const importSonioxEnvKey = () =>
  fdFetch<SharedSonioxKey>('/fd/soniox/import-env', { method: 'POST' })

// ── Google / Microsoft OAuth client (already one per deck, admin-managed) ──

export interface OAuthClientConfig {
  client_id: string
  client_id_set: boolean
  client_secret_set: boolean
}

export const getOAuthClientConfig = (provider: 'google' | 'microsoft') =>
  fdFetch<OAuthClientConfig>(`/fd/${provider}/config`)
