import { useCallback, useEffect, useState } from 'react'
import { Check, X, Trash2, Loader2, ClipboardList } from 'lucide-react'
import { useAuthStore } from '../../stores/authStore'

// Admin → Waitlist: people who applied from the sign-up page while
// self-registration is closed, each naming who recommended them. Approving
// creates the account with the password they chose
// (captain_claw/flight_deck/admin_routes.py approve_waitlist).

export interface WaitlistEntry {
  id: string
  email: string
  display_name: string
  referrer_name: string
  referrer_email: string
  cohort: string
  status: 'pending' | 'approved' | 'rejected'
  created_at: string
  decided_at: string
}

type Filter = 'pending' | 'all'

function _headers(): Record<string, string> {
  const { token } = useAuthStore.getState()
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: _headers(), credentials: 'include', ...init })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`)
  }
  return res.json()
}

export async function fetchWaitlist(status: Filter = 'pending'): Promise<{ entries: WaitlistEntry[]; pending: number }> {
  return call(`/fd/admin/waitlist?status=${status}`)
}

function when(iso: string): string {
  if (!iso) return ''
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

export function WaitlistAdmin({ refreshTick, onPendingCount, onApproved, note }: {
  refreshTick: number
  onPendingCount: (n: number) => void
  onApproved: () => void
  note: string | null
}) {
  const [filter, setFilter] = useState<Filter>('pending')
  const [entries, setEntries] = useState<WaitlistEntry[] | null>(null)
  const [busyId, setBusyId] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const data = await fetchWaitlist(filter)
      setEntries(data.entries)
      onPendingCount(data.pending)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [filter, onPendingCount])

  useEffect(() => { load() }, [load, refreshTick])

  const act = async (entry: WaitlistEntry, fn: () => Promise<unknown>, after?: () => void) => {
    setBusyId(entry.id)
    setError('')
    try {
      await fn()
      after?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyId('')
      load()
    }
  }

  const approve = (e: WaitlistEntry) =>
    act(e, () => call(`/fd/admin/waitlist/${e.id}/approve`, { method: 'POST', body: '{}' }), onApproved)
  const reject = (e: WaitlistEntry) => {
    if (!confirm(`Reject ${e.display_name || e.email}'s application? They can apply again later.`)) return
    act(e, () => call(`/fd/admin/waitlist/${e.id}/reject`, { method: 'POST' }))
  }
  const remove = (e: WaitlistEntry) => {
    if (!confirm(`Delete ${e.display_name || e.email}'s application from the list?`)) return
    act(e, () => call(`/fd/admin/waitlist/${e.id}`, { method: 'DELETE' }))
  }

  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="flex items-center gap-2 text-sm font-semibold text-zinc-200">
              <ClipboardList className="h-4 w-4 text-violet-400" />
              Waitlist
            </h3>
            <p className="text-xs text-zinc-500 mt-0.5">
              People who applied while registration is closed, with who recommended them. Approving creates
              their account with the password they chose, so they can sign in right away; the deck's
              kiosk default applies.
            </p>
          </div>
          <div className="inline-flex rounded-md border border-zinc-700 p-0.5" role="radiogroup" aria-label="Show">
            {(['pending', 'all'] as Filter[]).map((f) => (
              <button
                key={f}
                type="button"
                role="radio"
                aria-checked={filter === f}
                onClick={() => setFilter(f)}
                className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
                  filter === f ? 'bg-violet-600 text-white' : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200'
                }`}
              >
                {f === 'pending' ? 'Waiting' : 'All'}
              </button>
            ))}
          </div>
        </div>
        {note && <p className="text-xs text-amber-500/90">{note}</p>}
      </div>

      {error && (
        <div className="rounded-md border border-red-900/50 bg-red-950/20 px-4 py-2 text-sm text-red-400">{error}</div>
      )}

      {entries === null && !error && (
        <div className="flex justify-center py-6">
          <Loader2 className="h-5 w-5 animate-spin text-zinc-500" />
        </div>
      )}

      {entries?.map((e) => (
        <div
          key={e.id}
          className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3"
        >
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-zinc-100 truncate">{e.display_name || e.email}</span>
              {e.display_name && <span className="text-xs text-zinc-400 truncate">{e.email}</span>}
              {e.cohort && (
                <span className="rounded border border-violet-800/50 bg-violet-950/40 px-1.5 py-0.5 text-[10px] font-medium text-violet-300">
                  {e.cohort}
                </span>
              )}
              {e.status !== 'pending' && (
                <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                  e.status === 'approved' ? 'bg-emerald-950/60 text-emerald-400' : 'bg-zinc-800 text-zinc-400'
                }`}>
                  {e.status === 'approved' ? 'Approved' : 'Rejected'}
                </span>
              )}
            </div>
            <div className="text-xs text-zinc-400">
              Recommended by <span className="text-zinc-200">{e.referrer_name}</span>
              {e.referrer_email && <span className="text-zinc-500"> · {e.referrer_email}</span>}
            </div>
            <div className="text-[11px] text-zinc-600">
              Applied {when(e.created_at)}
              {e.decided_at && <> · {e.status} {when(e.decided_at)}</>}
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            {busyId === e.id && <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-500" />}
            {e.status === 'pending' ? (
              <>
                <button
                  onClick={() => approve(e)}
                  disabled={!!busyId}
                  className="flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-xs text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  <Check className="h-3 w-3" /> Approve
                </button>
                <button
                  onClick={() => reject(e)}
                  disabled={!!busyId}
                  className="flex items-center gap-1 rounded-md bg-zinc-800 px-2.5 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:opacity-50"
                >
                  <X className="h-3 w-3" /> Reject
                </button>
              </>
            ) : (
              <button
                onClick={() => remove(e)}
                disabled={!!busyId}
                title="Delete application"
                className="rounded-md p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-red-400 disabled:opacity-50"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>
      ))}

      {entries?.length === 0 && (
        <p className="text-center text-sm text-zinc-500 py-8">
          {filter === 'pending' ? 'Nobody is waiting for approval.' : 'No applications yet.'}
        </p>
      )}
    </div>
  )
}
