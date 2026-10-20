import { create } from 'zustand'
import { registerSignOutTeardown, useAuthStore } from './authStore'

export type MCPTransport = 'http' | 'stdio'
// '' = none, or client_credentials when client_id + token_endpoint are set;
// 'oauth' = interactive sign-in (discovery + Dynamic Client Registration).
export type MCPAuthMode = '' | 'oauth'

export interface MCPOAuthStatus {
  connected: boolean
  connected_by?: string
  connected_at?: number
  expires_at?: number
  issuer?: string
  scope?: string
  registered?: boolean
}

export interface MCPServer {
  name: string
  // Common
  transport?: MCPTransport
  enabled: boolean
  allowed_agents?: string[]
  headers: Record<string, string>
  added_at?: number
  initialized?: boolean
  tools_count?: number
  last_error?: string | null
  // HTTP-only
  url: string
  auth?: MCPAuthMode
  scope?: string
  oauth?: MCPOAuthStatus
  client_id: string
  client_secret: string
  client_secret_set?: boolean
  token_endpoint: string
  // stdio-only
  command?: string
  args?: string[]
  env?: Record<string, string>
}

export interface MCPProbeResult {
  ok: boolean
  tools_count?: number
  tool_names?: string[]
  error?: string
  status_code?: number | null
  // OAuth server nobody has signed in to yet: discovery result instead of tools.
  needs_sign_in?: boolean
  issuer?: string
  registration_supported?: boolean
}

export interface MCPServerInput {
  name: string
  transport?: MCPTransport
  enabled?: boolean
  allowed_agents?: string[]
  headers?: Record<string, string>
  // HTTP
  url?: string
  auth?: MCPAuthMode
  scope?: string
  client_id?: string
  client_secret?: string
  token_endpoint?: string
  // stdio
  command?: string
  args?: string[]
  env?: Record<string, string>
}

interface MCPStore {
  servers: MCPServer[]
  loading: boolean
  saving: boolean
  testing: Record<string, boolean>
  error: string | null
  notice: string | null
  lastTestResult: Record<string, MCPProbeResult>

  refresh: () => Promise<void>
  // ``popup`` is a window opened synchronously in the click handler (popup
  // blockers allow only that); without one, signIn opens it itself.
  signIn: (name: string, popup?: Window | null) => Promise<void>
  signOut: (name: string) => Promise<void>
  startMessageListener: () => () => void
  saveServer: (input: MCPServerInput) => Promise<MCPServer | null>
  removeServer: (name: string) => Promise<boolean>
  testServer: (name: string) => Promise<MCPProbeResult>
  probeTransient: (input: MCPServerInput) => Promise<MCPProbeResult>
}

function authHeaders(): Record<string, string> {
  const { token } = useAuthStore.getState()
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

async function fetchJson(url: string, init?: RequestInit): Promise<any> {
  const resp = await fetch(url, {
    credentials: 'include',
    ...init,
    headers: {
      ...authHeaders(),
      ...(init?.headers || {}),
    },
  })
  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`)
  }
  return resp.json()
}

const _OAUTH_MESSAGE_TYPE = 'captain-claw-mcp-oauth'

// The Electron shell (desktop/preload.js) hands every http(s) window.open to
// the system browser, where the sign-in ticket cookie isn't — same as Google.
function _isDesktopShell(): boolean {
  return !!(window as Window & { captainClawDesktop?: { isDesktop?: boolean } }).captainClawDesktop?.isDesktop
}

/** Open the sign-in popup — call synchronously from a click handler. */
export function openSignInPopup(): Window | null {
  if (_isDesktopShell()) return null
  const w = 520
  const h = 680
  const left = window.screenX + (window.outerWidth - w) / 2
  const top = window.screenY + (window.outerHeight - h) / 2
  return window.open('about:blank', 'captain-claw-mcp-oauth', `width=${w},height=${h},left=${left},top=${top}`)
}

let _popup: Window | null = null
let _popupWatch: ReturnType<typeof setInterval> | null = null

function _watchPopup(popup: Window, onClosed: () => void): void {
  if (_popupWatch) clearInterval(_popupWatch)
  _popup = popup
  const started = Date.now()
  _popupWatch = setInterval(() => {
    if (popup.closed || Date.now() - started > 10 * 60_000) {
      if (_popupWatch) clearInterval(_popupWatch)
      _popupWatch = null
      _popup = null
      if (popup.closed) onClosed()
    }
  }, 500)
}

// A Flight Deck sign-out closes a pending sign-in, so the next person at the
// screen can't finish it under the outgoing user's name.
registerSignOutTeardown(() => {
  if (_popupWatch) clearInterval(_popupWatch)
  _popupWatch = null
  if (_popup && !_popup.closed) {
    try { _popup.close() } catch { /* already gone */ }
  }
  _popup = null
})

export const useMCPStore = create<MCPStore>((set, get) => ({
  servers: [],
  loading: false,
  saving: false,
  testing: {},
  error: null,
  notice: null,
  lastTestResult: {},

  refresh: async () => {
    set({ loading: true, error: null })
    try {
      const data = await fetchJson('/fd/mcp/servers')
      set({
        servers: (data?.servers as MCPServer[]) || [],
        loading: false,
      })
    } catch (exc) {
      set({
        loading: false,
        error: exc instanceof Error ? exc.message : String(exc),
      })
    }
  },

  saveServer: async (input) => {
    set({ saving: true, error: null })
    try {
      const data = await fetchJson('/fd/mcp/servers', {
        method: 'POST',
        body: JSON.stringify(input),
      })
      await get().refresh()
      set({ saving: false })
      return (data?.server as MCPServer) || null
    } catch (exc) {
      set({
        saving: false,
        error: exc instanceof Error ? exc.message : String(exc),
      })
      return null
    }
  },

  removeServer: async (name) => {
    set({ error: null })
    try {
      await fetchJson(`/fd/mcp/servers/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      })
      await get().refresh()
      return true
    } catch (exc) {
      set({ error: exc instanceof Error ? exc.message : String(exc) })
      return false
    }
  },

  testServer: async (name) => {
    set((state) => ({
      testing: { ...state.testing, [name]: true },
      error: null,
    }))
    try {
      const result = (await fetchJson(
        `/fd/mcp/servers/${encodeURIComponent(name)}/test`,
        { method: 'POST' },
      )) as MCPProbeResult
      set((state) => ({
        testing: { ...state.testing, [name]: false },
        lastTestResult: { ...state.lastTestResult, [name]: result },
      }))
      // Refresh status fields (initialized / tools_count) after a successful probe.
      get().refresh()
      return result
    } catch (exc) {
      const result: MCPProbeResult = {
        ok: false,
        error: exc instanceof Error ? exc.message : String(exc),
      }
      set((state) => ({
        testing: { ...state.testing, [name]: false },
        lastTestResult: { ...state.lastTestResult, [name]: result },
      }))
      return result
    }
  },

  signIn: async (name, preopened) => {
    set({ error: null, notice: null })
    if (_isDesktopShell()) {
      preopened?.close()
      set({ error: "The desktop app can't run the sign-in — open Flight Deck in a web browser to sign in to this MCP server." })
      return
    }
    const popup = preopened ?? openSignInPopup()
    if (!popup) {
      set({ error: 'The browser blocked the sign-in popup — allow popups for this site and try again.' })
      return
    }
    let ticket = ''
    try {
      const data = await fetchJson('/fd/mcp/oauth/connect-ticket', {
        method: 'POST',
        body: JSON.stringify({ server: name }),
      })
      ticket = typeof data?.ticket === 'string' ? data.ticket : ''
      if (!ticket) throw new Error('Flight Deck returned no sign-in ticket.')
    } catch (exc) {
      popup.close()
      set({ error: `Couldn't start sign-in: ${exc instanceof Error ? exc.message : String(exc)}` })
      return
    }
    if (popup.closed) return
    popup.location.href = `${window.location.origin}/fd/mcp/oauth/login?ticket=${encodeURIComponent(ticket)}`
    _watchPopup(popup, () => { get().refresh() })
  },

  signOut: async (name) => {
    set({ error: null, notice: null })
    try {
      await fetchJson('/fd/mcp/oauth/disconnect', {
        method: 'POST',
        body: JSON.stringify({ server: name }),
      })
      set((state) => {
        const lastTestResult = { ...state.lastTestResult }
        delete lastTestResult[name]
        return { lastTestResult, notice: `Signed out of ${name}.` }
      })
      await get().refresh()
    } catch (exc) {
      set({ error: exc instanceof Error ? exc.message : String(exc) })
    }
  },

  startMessageListener: () => {
    const handler = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return
      const data = event.data
      if (!data || typeof data !== 'object' || data.type !== _OAUTH_MESSAGE_TYPE) return
      if (data.status === 'success') {
        set({ notice: data.detail || 'Signed in.', error: null })
      } else {
        set({ error: `${data.title || 'Sign-in failed'}${data.detail ? ` — ${data.detail}` : ''}` })
      }
      setTimeout(() => { get().refresh() }, 300)
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  },

  probeTransient: async (input) => {
    try {
      const result = (await fetchJson('/fd/mcp/probe', {
        method: 'POST',
        body: JSON.stringify(input),
      })) as MCPProbeResult
      return result
    } catch (exc) {
      return {
        ok: false,
        error: exc instanceof Error ? exc.message : String(exc),
      }
    }
  },
}))
