import { useCallback, useEffect, useState } from 'react'
import { ClipboardList, MailPlus, RefreshCw, UserCog, Users } from 'lucide-react'
import { useAuthStore } from '../stores/authStore'
import { UsersAdmin } from '../components/admin/UsersAdmin'
import { InvitesAdmin } from '../components/admin/InvitesAdmin'
import { WaitlistAdmin } from '../components/admin/WaitlistAdmin'
import {
  CONFIG_DEFAULTS, USERS_TAB_KEY, fetchConfig, kioskModeNote, type SystemConfig, type UsersTab,
} from '../components/admin/deckConfig'

// Who is on the deck and who is coming: accounts, invitations and the waitlist
// (admin only). The deck-wide settings these follow — kiosk mode, the
// waitlist switch, plans — stay on the Admin page.

function takeRequestedTab(): UsersTab {
  try {
    const t = sessionStorage.getItem(USERS_TAB_KEY)
    sessionStorage.removeItem(USERS_TAB_KEY)
    if (t === 'invitations' || t === 'waitlist') return t
  } catch { /* storage blocked */ }
  return 'users'
}

export function UsersPage() {
  const currentUser = useAuthStore((s) => s.user)
  const signUpMode = useAuthStore((s) => s.signUp.mode)
  const [tab, setTab] = useState<UsersTab>(takeRequestedTab)
  const [config, setConfig] = useState<SystemConfig>({ ...CONFIG_DEFAULTS })
  const [loading, setLoading] = useState(true)
  // Bumped by Refresh: each tab loads on its own and reloads on it.
  const [refreshTick, setRefreshTick] = useState(0)
  // Bumped when a waitlist approval creates an account, so the Users list shows it.
  const [usersTick, setUsersTick] = useState(0)
  const [totalUsers, setTotalUsers] = useState<number | null>(null)
  const [openInvites, setOpenInvites] = useState<number | null>(null)
  const [waitlistPending, setWaitlistPending] = useState(0)

  const loadConfig = useCallback(async () => {
    setLoading(true)
    try { setConfig(await fetchConfig()) } catch { /* keep what we have */ } finally { setLoading(false) }
  }, [])

  useEffect(() => { loadConfig() }, [loadConfig, refreshTick])

  const onApproved = useCallback(() => setUsersTick((t) => t + 1), [])

  if (!currentUser || currentUser.role !== 'admin') {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-zinc-500 text-sm">Admin access required</div>
      </div>
    )
  }

  const tabClass = (t: UsersTab) => `flex shrink-0 items-center gap-2 px-3 sm:px-4 py-2.5 text-sm border-b-2 transition-colors ${
    tab === t ? 'border-violet-500 text-zinc-100' : 'border-transparent text-zinc-500 hover:text-zinc-300'
  }`
  const count = (n: number | null) => n !== null && <span className="tabular-nums text-zinc-500">{n}</span>

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-4xl mx-auto p-4 sm:p-6 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <UserCog className="h-5 w-5 text-violet-400" />
            <h1 className="text-lg font-semibold text-zinc-100">Users</h1>
          </div>
          <button
            onClick={() => setRefreshTick((t) => t + 1)}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        <div className="flex gap-1 overflow-x-auto border-b border-zinc-800" role="tablist">
          <button role="tab" aria-selected={tab === 'users'} onClick={() => setTab('users')} className={tabClass('users')}>
            <Users className="hidden h-4 w-4 sm:block" />
            Users {count(totalUsers)}
          </button>
          <button role="tab" aria-selected={tab === 'invitations'} onClick={() => setTab('invitations')} className={tabClass('invitations')}>
            <MailPlus className="hidden h-4 w-4 sm:block" />
            Invitations {count(openInvites)}
          </button>
          <button role="tab" aria-selected={tab === 'waitlist'} onClick={() => setTab('waitlist')} className={tabClass('waitlist')}>
            <ClipboardList className="hidden h-4 w-4 sm:block" />
            Waitlist
            {waitlistPending > 0 && (
              <span className="rounded-full bg-violet-600 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white">
                {waitlistPending}
              </span>
            )}
          </button>
        </div>

        {/* All three stay mounted, so the tab counts are right from the start and
            switching tabs keeps a search, its filters or a half-written invite. */}
        <div hidden={tab !== 'users'}>
          <UsersAdmin refreshTick={refreshTick + usersTick} config={config} onTotal={setTotalUsers} />
        </div>
        <div hidden={tab !== 'invitations'}>
          <InvitesAdmin
            refreshTick={refreshTick}
            kioskDefault={config.kiosk_new_users}
            kioskNote={kioskModeNote(config.kiosk_mode)}
            onOpenCount={setOpenInvites}
          />
        </div>
        <div hidden={tab !== 'waitlist'}>
          <WaitlistAdmin
            refreshTick={refreshTick}
            onPendingCount={setWaitlistPending}
            onApproved={onApproved}
            note={
              signUpMode === 'register'
                ? 'Self-registration is open (FD_REGISTRATION_OPEN), so the sign-up page creates accounts directly and takes no applications.'
                : !config.waitlist_enabled
                  ? 'The waitlist is off (Admin → Settings), so the sign-up page takes no new applications.'
                  : null
            }
          />
        </div>
      </div>
    </div>
  )
}
