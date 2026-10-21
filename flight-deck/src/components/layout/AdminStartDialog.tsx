import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { ChevronRight, Monitor, Settings2, UserCog, type LucideIcon } from 'lucide-react'
import { useAuthStore } from '../../stores/authStore'
import { useUIStore } from '../../stores/uiStore'
import { useChatStore } from '../../stores/chatStore'
import { ADMIN_TAB_KEY } from '../../services/sharedIntegrations'

// Right after an admin signs in (not on a reload): where to start — the deck's
// settings (Admin → Settings), the Users page, or the agent desktop. Closing
// it (Esc, outside, ✕) is the agent desktop too.

type Start = 'settings' | 'users' | 'desktop'

const STARTS: { id: Start; icon: LucideIcon; label: string; hint: string }[] = [
  { id: 'settings', icon: Settings2, label: 'Settings', hint: 'Branding, kiosk mode, the waitlist, plans and integrations' },
  { id: 'users', icon: UserCog, label: 'Users', hint: 'Accounts, invitations and waitlist applications' },
  { id: 'desktop', icon: Monitor, label: 'Flight Deck', hint: 'The agent desktop' },
]

const close = () => useAuthStore.getState().setAdminStartPending(false)

export function AdminStartDialog() {
  const name = useAuthStore((s) => s.user?.display_name || '')

  const go = (start: Start) => {
    close()
    if (start === 'desktop') {
      useUIStore.getState().setView('desktop')
      return
    }
    // Settings and Users are pages of the full layout (as SimpleLayout's links).
    if (start === 'settings') {
      try { sessionStorage.setItem(ADMIN_TAB_KEY, 'settings') } catch { /* storage blocked */ }
    }
    useChatStore.setState({ chatFullscreen: false })
    useUIStore.getState().setLayoutMode('full')
    useUIStore.getState().setView(start === 'settings' ? 'admin' : 'users')
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={close}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="fd-admin-start-title"
        className="w-full max-w-2xl rounded-2xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl wide:p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="fd-admin-start-title" className="text-lg font-semibold text-zinc-100">
          {name ? `Welcome back, ${name}` : 'Welcome back'}
        </h2>
        <p className="mt-1 text-sm text-zinc-500">Where do you want to start?</p>

        <div className="mt-5 grid gap-3 wide:grid-cols-3">
          {STARTS.map(({ id, icon: Icon, label, hint }, i) => (
            <button
              key={id}
              autoFocus={i === 0}
              onClick={() => go(id)}
              className="group flex items-center gap-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 text-left transition-colors hover:border-violet-500/40 hover:bg-violet-500/5 focus-visible:border-violet-500/60 focus-visible:outline-none wide:flex-col wide:items-start wide:gap-4"
            >
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-violet-500/15 text-violet-400">
                <Icon className="h-5 w-5" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-medium text-zinc-100">{label}</span>
                <span className="mt-0.5 block text-xs leading-snug text-zinc-500">{hint}</span>
              </span>
              <ChevronRight className="h-4 w-4 shrink-0 text-zinc-600 group-hover:text-zinc-400 wide:hidden" />
            </button>
          ))}
        </div>
      </div>
    </div>,
    document.body,
  )
}
