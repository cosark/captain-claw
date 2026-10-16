import { useState, type FormEvent } from 'react'
import { KeyRound, Loader2 } from 'lucide-react'
import { useAuthStore } from '../../stores/authStore'
import { changeOwnPassword } from '../../services/profile'

export const MIN_PASSWORD_LENGTH = 6

// The signed-in user changes their own password (Profile page and the kiosk's
// Profile dialog). Needs the current one; optionally ends their other sessions.
// An admin resets someone else's from Admin → Users instead.
export function ChangePasswordCard() {
  const email = useAuthStore((s) => s.user?.email ?? '')
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [signOutOthers, setSignOutOthers] = useState(false)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const tooShort = next.length > 0 && next.length < MIN_PASSWORD_LENGTH
  const mismatch = confirm.length > 0 && confirm !== next
  const valid = !!current && next.length >= MIN_PASSWORD_LENGTH && confirm === next

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!valid || saving) return
    setSaving(true); setError(null); setNotice(null)
    try {
      await changeOwnPassword(current, next, signOutOthers)
      setCurrent(''); setNext(''); setConfirm('')
      setNotice(signOutOthers
        ? 'Password changed. Your other devices are signed out.'
        : 'Password changed.')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const input = 'w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none'

  return (
    <form onSubmit={submit} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-5 space-y-4">
      <div>
        <h3 className="flex items-center gap-2 text-sm font-semibold text-zinc-200">
          <KeyRound className="h-4 w-4 text-violet-500" />
          Password
        </h3>
        <p className="text-xs text-zinc-500 mt-0.5">Change the password you sign in with.</p>
      </div>
      {/* For password managers: which account this password belongs to. */}
      <input type="text" name="username" autoComplete="username" value={email} readOnly hidden />
      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label className="block text-xs font-medium text-zinc-500 mb-1">Current password</label>
          <input
            type="password"
            value={current}
            onChange={(e) => { setCurrent(e.target.value); setNotice(null) }}
            autoComplete="current-password"
            className={input}
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-zinc-500 mb-1">New password</label>
          <input
            type="password"
            value={next}
            onChange={(e) => { setNext(e.target.value); setNotice(null) }}
            placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
            autoComplete="new-password"
            className={input}
          />
          {tooShort && (
            <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-500/80">At least {MIN_PASSWORD_LENGTH} characters.</p>
          )}
        </div>
        <div>
          <label className="block text-xs font-medium text-zinc-500 mb-1">Confirm new password</label>
          <input
            type="password"
            value={confirm}
            onChange={(e) => { setConfirm(e.target.value); setNotice(null) }}
            autoComplete="new-password"
            className={input}
          />
          {mismatch && (
            <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-500/80">Doesn't match.</p>
          )}
        </div>
      </div>
      <label className="flex items-center gap-2 text-xs text-zinc-400 cursor-pointer">
        <input
          type="checkbox"
          checked={signOutOthers}
          onChange={(e) => setSignOutOthers(e.target.checked)}
          className="h-3.5 w-3.5 rounded border-zinc-600 bg-zinc-950 accent-violet-600"
        />
        Sign out my other devices and browsers
      </label>
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={!valid || saving}
          className="flex items-center gap-1.5 rounded-md bg-violet-600 px-3 py-1.5 text-xs text-white hover:bg-violet-500 disabled:opacity-50"
        >
          {saving && <Loader2 className="h-3 w-3 animate-spin" />}
          {saving ? 'Changing…' : 'Change password'}
        </button>
      </div>
      {notice && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-300">
          {notice}
        </div>
      )}
      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
          {error}
        </div>
      )}
    </form>
  )
}
