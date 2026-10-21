import { create } from 'zustand'
import { useUIStore } from './uiStore'
import { useAuthStore } from './authStore'
import type { ViewMode } from '../types'
import { USERS_TAB_KEY, type UsersTab } from '../components/admin/deckConfig'
import { ADMIN_TAB_KEY } from '../services/sharedIntegrations'

// The Admin console (layoutMode 'console'): the deck's admin work on pages of
// its own — no chat, no agent desktop. Each page has an address,
// <deck>/console/<page>, so a link (or a desktop app) can open it straight
// away, and Back / Forward move between pages.

export type ConsolePage = 'overview' | 'models' | 'archetypes' | 'connections' | 'users' | 'usage' | 'settings'

export const CONSOLE_PAGES: ConsolePage[] = ['overview', 'models', 'archetypes', 'connections', 'users', 'usage', 'settings']

export const CONSOLE_PATH = '/console'

// The page this browser had open last — a per-viewer convenience only.
const LAST_PAGE_KEY = 'fd:console-page'

/** The console page an address asks for, or null when it isn't a console address. */
export function pageFromPath(path: string): ConsolePage | null {
  if (path !== CONSOLE_PATH && !path.startsWith(`${CONSOLE_PATH}/`)) return null
  const p = path.slice(CONSOLE_PATH.length + 1).split('/')[0] as ConsolePage
  return CONSOLE_PAGES.includes(p) ? p : 'overview'
}

function lastPage(): ConsolePage {
  try {
    const p = localStorage.getItem(LAST_PAGE_KEY) as ConsolePage | null
    if (p && CONSOLE_PAGES.includes(p)) return p
  } catch { /* storage blocked */ }
  return 'overview'
}

function remember(page: ConsolePage) {
  try { localStorage.setItem(LAST_PAGE_KEY, page) } catch { /* storage blocked */ }
}

/** Where a page should open when another page sends you there. */
export interface ConsoleIntent {
  usersTab?: UsersTab
  connection?: string
}

interface ConsoleStore {
  page: ConsolePage
  // The console was asked for — the address bar (<deck>/console…), a sign-in,
  // a click: it shows for an admin whatever layout their saved settings bring.
  requested: boolean
  intent: ConsoleIntent
  go: (page: ConsolePage, intent?: ConsoleIntent) => void
  /** A page took its intent: don't apply it again on the next visit. */
  takeIntent: () => ConsoleIntent
}

const initialPage = pageFromPath(window.location.pathname)

export const useConsoleStore = create<ConsoleStore>((set, get) => ({
  page: initialPage ?? lastPage(),
  requested: initialPage !== null,
  intent: {},

  go: (page, intent = {}) => {
    remember(page)
    if (window.location.pathname !== `${CONSOLE_PATH}/${page}`) {
      window.history.pushState(null, '', `${CONSOLE_PATH}/${page}`)
    }
    set({ page, intent })
  },
  takeIntent: () => {
    const intent = get().intent
    if (intent.usersTab || intent.connection) set({ intent: {} })
    return intent
  },
}))

/** Who may use the console: a signed-in admin, or on a deck without sign-in
 *  (the desktop app) whoever runs it — the server treats them as its admin. */
export function selectConsoleAdmin(s: ReturnType<typeof useAuthStore.getState>): boolean {
  return s.authEnabled === false || (s.authEnabled === true && s.user?.role === 'admin')
}

/** Open the Admin console (admins only — App falls back to the usual layout otherwise). */
export function openConsole(page?: ConsolePage, intent?: ConsoleIntent) {
  useConsoleStore.setState({ requested: true })
  useUIStore.getState().setLayoutMode('console')
  useConsoleStore.getState().go(page ?? useConsoleStore.getState().page, intent)
}

/** Back to Flight Deck — its full layout (optionally on one of its pages), or
 *  the chat-first simple one (the desktop app's "User" view). */
export function leaveConsole(view?: ViewMode, layout: 'full' | 'simple' = 'full') {
  useConsoleStore.setState({ requested: false })
  if (pageFromPath(window.location.pathname)) window.history.pushState(null, '', '/')
  useUIStore.getState().setLayoutMode(layout)
  if (view) useUIStore.getState().setView(view)
}

/** Back / Forward: into the console, to another of its pages, or out of it. */
export function syncConsoleFromUrl() {
  const page = pageFromPath(window.location.pathname)
  if (page) {
    remember(page)
    useConsoleStore.setState({ page, requested: true })
    return
  }
  const ui = useUIStore.getState()
  if (!useConsoleStore.getState().requested && ui.layoutMode !== 'console') return
  useConsoleStore.setState({ requested: false })
  ui.setLayoutMode('full')
}

// The cards the console reuses link to Flight Deck pages (setView): "Admin →
// Integrations", "Connections → MCP Servers", the Users page. While the console
// shows, such a link opens the console's own page for it, or leaves the
// console for any other page.
let restoringView = false
useUIStore.subscribe((s, prev) => {
  if (restoringView || s.view === prev.view || s.layoutMode !== 'console') return
  const { go } = useConsoleStore.getState()
  const take = (key: string) => {
    try { const v = sessionStorage.getItem(key); sessionStorage.removeItem(key); return v } catch { return null }
  }
  if (s.view === 'users') {
    const tab = take(USERS_TAB_KEY)
    go('users', tab === 'invitations' || tab === 'waitlist' ? { usersTab: tab } : {})
  } else if (s.view === 'admin') {
    // The one link to Admin → Integrations is Telegram's "add the team bot".
    if (take(ADMIN_TAB_KEY) === 'integrations') go('connections', { connection: 'telegram' })
    else go('settings')
  } else if (s.view === 'connections') {
    // ConnectionsPage's remembered tab, which these links set first.
    let tab = ''
    try { tab = localStorage.getItem('fd:connections-tab') || '' } catch { /* storage blocked */ }
    go('connections', tab ? { connection: tab } : {})
  } else if (s.view === 'library') {
    go('models')
  } else {
    leaveConsole(s.view)
    return
  }
  // Put the view back, so the same link works again next time.
  restoringView = true
  useUIStore.setState({ view: prev.view })
  restoringView = false
})

/** Someone who isn't an admin followed a console address: show the usual one. */
export function dropConsoleRequest() {
  if (!useConsoleStore.getState().requested) return
  useConsoleStore.setState({ requested: false })
  if (pageFromPath(window.location.pathname)) window.history.replaceState(null, '', '/')
}
