import { create } from 'zustand'
import { useAuthStore, refreshAccessToken, registerSignOutTeardown } from './authStore'

// The Microsoft (Outlook / OneDrive / Calendar) twin of googleAuthStore: the
// admin's app registration (client id + secret, tenant, scopes) and each
// user's own connection, through /fd/microsoft/*.

export interface MicrosoftGrantedScope {
  scope: string
  label: string
}

export interface MicrosoftUserInfo {
  sub?: string
  email?: string
  name?: string
  upn?: string
}

export interface MicrosoftAuthStatus {
  configured: boolean
  connected: boolean
  user: MicrosoftUserInfo | null
  granted_scopes: MicrosoftGrantedScope[]
  tenant: string
  redirect_uri: string
}

export interface MicrosoftScopeEntry {
  scope: string
  label: string
  description: string
  sensitivity: 'none' | 'sensitive' | 'restricted'
  group: string
}

export interface MicrosoftAuthConfig {
  client_id: string
  client_id_set: boolean
  client_secret_set: boolean
  tenant: string
  scopes: string[]
  default_scopes: string[]
  required_scopes: string[]
  scope_catalog: MicrosoftScopeEntry[]
  redirect_uri: string
}

interface MicrosoftAuthStore {
  status: MicrosoftAuthStatus | null
  config: MicrosoftAuthConfig | null
  loading: boolean
  error: string | null
  lastPopupMessage: string | null
  // The agentic signature (GET/PUT /fd/microsoft/mail-signature): plain text
  // the agents add to the end of every Outlook draft. null = not loaded yet.
  signature: string | null
  signatureError: string | null

  refresh: () => Promise<void>
  syncStatus: () => Promise<void>
  saveConfig: (patch: Partial<{
    client_id: string
    client_secret: string
    tenant: string
    scopes: string[]
  }>) => Promise<boolean>
  clearCredentials: () => Promise<boolean>
  connect: () => Promise<void>
  disconnect: () => Promise<void>
  startMessageListener: () => () => void
  fetchSignature: () => Promise<void>
  saveSignature: (signature: string) => Promise<boolean>
}

const emptyStatus: MicrosoftAuthStatus = {
  configured: false,
  connected: false,
  user: null,
  granted_scopes: [],
  tenant: '',
  redirect_uri: '',
}

function authHeaders(): Record<string, string> {
  const { token } = useAuthStore.getState()
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

class HttpError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, statusText: string, text: string) {
    super(`${status} ${statusText}: ${text}`)
    this.status = status
    let detail = ''
    try {
      const parsed = JSON.parse(text)
      if (typeof parsed?.detail === 'string') detail = parsed.detail
    } catch { /* not JSON */ }
    this.detail = detail
  }
}

async function fetchJson(url: string, init?: RequestInit): Promise<any> {
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
    throw new HttpError(resp.status, resp.statusText, text)
  }
  return resp.json()
}

function _errorText(exc: unknown): string {
  if (exc instanceof HttpError) {
    // A deck that predates /fd/microsoft answers 404 (or the SPA catch-all).
    if (exc.status === 404) return "This Flight Deck doesn't offer Microsoft yet — it needs an update and a restart."
    if (exc.detail) return exc.detail
  }
  return exc instanceof Error ? exc.message : String(exc)
}

// Same single-use connect ticket as Google (see googleAuthStore): the popup
// can't carry the Bearer token, and the ticket only works in this browser.
async function _mintConnectTicket(): Promise<string> {
  const data = await fetchJson('/fd/microsoft/connect-ticket', { method: 'POST', body: '{}' })
  const ticket = typeof data?.ticket === 'string' ? data.ticket : ''
  if (!ticket) throw new Error('Flight Deck returned no sign-in ticket.')
  return ticket
}

let _popup: Window | null = null
let _popupWatch: ReturnType<typeof setInterval> | null = null
const _POPUP_POLL_MS = 500
const _POPUP_WATCH_MAX_MS = 10 * 60_000

function _forgetPopup(close: boolean): void {
  if (_popupWatch) clearInterval(_popupWatch)
  _popupWatch = null
  if (close && _popup && !_popup.closed) {
    try { _popup.close() } catch { /* already gone */ }
  }
  _popup = null
}

function _watchPopup(popup: Window, onClosed: () => void): void {
  _forgetPopup(false)
  _popup = popup
  const started = Date.now()
  _popupWatch = setInterval(() => {
    if (popup.closed) {
      _forgetPopup(false)
      onClosed()
    } else if (Date.now() - started > _POPUP_WATCH_MAX_MS) {
      if (_popupWatch) clearInterval(_popupWatch)
      _popupWatch = null
    }
  }, _POPUP_POLL_MS)
}

// A sign-out closes a pending Microsoft sign-in: the next person at the screen
// must not finish it into the outgoing user's account.
registerSignOutTeardown(() => _forgetPopup(true))

function _isDesktopShell(): boolean {
  return !!(window as Window & { captainClawDesktop?: { isDesktop?: boolean } }).captainClawDesktop?.isDesktop
}

const _POPUP_BLOCKED = 'The browser blocked the Microsoft sign-in popup — allow popups for this site and try again.'
const _SESSION_EXPIRED = 'Your Flight Deck session has expired — sign in again, then connect Microsoft.'
const _AUTH_OFF = "Microsoft isn't available on this deck: Flight Deck sign-in is turned off."
const _DESKTOP_SHELL = "The desktop app can't run the Microsoft sign-in — open Flight Deck in a web browser to connect Microsoft."

function _connectError(exc: unknown): string {
  if (exc instanceof HttpError && exc.status === 401) return _SESSION_EXPIRED
  return `Couldn't start Microsoft sign-in: ${_errorText(exc)}`
}

export const useMicrosoftAuthStore = create<MicrosoftAuthStore>((set, get) => ({
  status: null,
  config: null,
  loading: false,
  error: null,
  lastPopupMessage: null,
  signature: null,
  signatureError: null,

  refresh: async () => {
    if (useAuthStore.getState().authEnabled === false) {
      set({ status: emptyStatus, config: null, loading: false, error: null })
      return
    }
    set({ loading: true, error: null })
    try {
      const [status, config] = await Promise.all([
        fetchJson('/fd/microsoft/status'),
        fetchJson('/fd/microsoft/config'),
      ])
      set({
        status: status as MicrosoftAuthStatus,
        config: config as MicrosoftAuthConfig,
        loading: false,
      })
    } catch (exc) {
      set({ loading: false, error: _errorText(exc), status: get().status || emptyStatus })
    }
  },

  syncStatus: async () => {
    try {
      const status = (await fetchJson('/fd/microsoft/status')) as MicrosoftAuthStatus
      const justConnected = !!status?.connected && !get().status?.connected
      set({
        status,
        ...(justConnected
          ? { lastPopupMessage: `Connected${status.user?.email ? ` as ${status.user.email}` : ''}` }
          : {}),
      })
    } catch { /* the next explicit refresh() reports it */ }
  },

  saveConfig: async (patch) => {
    set({ loading: true, error: null })
    try {
      await fetchJson('/fd/microsoft/config', { method: 'POST', body: JSON.stringify(patch) })
      await get().refresh()
      return true
    } catch (exc) {
      set({ loading: false, error: _errorText(exc) })
      return false
    }
  },

  clearCredentials: async () => {
    set({ loading: true, error: null })
    try {
      await fetchJson('/fd/microsoft/config', { method: 'POST', body: JSON.stringify({ clear: true }) })
      await get().refresh()
      return true
    } catch (exc) {
      set({ loading: false, error: _errorText(exc) })
      return false
    }
  },

  connect: async () => {
    set({ error: null })
    if (!useAuthStore.getState().authEnabled) {
      set({ error: _AUTH_OFF })
      return
    }
    if (_isDesktopShell()) {
      set({ error: _DESKTOP_SHELL })
      return
    }
    const w = 520
    const h = 680
    const left = window.screenX + (window.outerWidth - w) / 2
    const top = window.screenY + (window.outerHeight - h) / 2
    const popup = window.open('about:blank', 'captain-claw-microsoft-oauth', `width=${w},height=${h},left=${left},top=${top}`)
    if (!popup) {
      set({ error: _POPUP_BLOCKED })
      return
    }
    let ticket: string
    try {
      ticket = await _mintConnectTicket()
    } catch (exc) {
      popup.close()
      set({ error: _connectError(exc) })
      return
    }
    if (popup.closed || !useAuthStore.getState().isAuthenticated) {
      if (!popup.closed) popup.close()
      return
    }
    popup.location.href = `${window.location.origin}/fd/microsoft/login?ticket=${encodeURIComponent(ticket)}`
    _watchPopup(popup, () => { get().syncStatus() })
  },

  disconnect: async () => {
    set({ loading: true, error: null })
    try {
      const data = await fetchJson('/fd/microsoft/logout', { method: 'POST' })
      await get().refresh()
      if (typeof data?.note === 'string') set({ lastPopupMessage: `Disconnected. ${data.note}` })
    } catch (exc) {
      set({ loading: false, error: _errorText(exc) })
    }
  },

  fetchSignature: async () => {
    set({ signatureError: null })
    try {
      const data = await fetchJson('/fd/microsoft/mail-signature')
      set({ signature: typeof data?.signature === 'string' ? data.signature : '' })
    } catch (exc) {
      set({ signatureError: _errorText(exc) })
    }
  },

  saveSignature: async (signature) => {
    set({ signatureError: null })
    try {
      const data = await fetchJson('/fd/microsoft/mail-signature', {
        method: 'PUT',
        body: JSON.stringify({ signature }),
      })
      set({ signature: typeof data?.signature === 'string' ? data.signature : '' })
      return true
    } catch (exc) {
      set({ signatureError: _errorText(exc) })
      return false
    }
  },

  startMessageListener: () => {
    const handler = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return
      const data = event.data
      if (!data || typeof data !== 'object') return
      if (data.type !== 'captain-claw-microsoft-oauth') return
      set({
        lastPopupMessage:
          data.status === 'success'
            ? `Connected${data.email ? ` as ${data.email}` : ''}`
            : `Error: ${data.title || 'Sign-in failed'}${data.detail ? ` — ${data.detail}` : ''}`,
      })
      setTimeout(() => { get().refresh() }, 300)
    }
    const onFocus = () => { get().syncStatus() }
    window.addEventListener('message', handler)
    window.addEventListener('focus', onFocus)
    return () => {
      window.removeEventListener('message', handler)
      window.removeEventListener('focus', onFocus)
    }
  },
}))

registerSignOutTeardown(() => {
  useMicrosoftAuthStore.setState({
    status: null, config: null, lastPopupMessage: null, error: null, signature: null, signatureError: null,
  })
})
