import { useState } from 'react'
import { ArrowRight, KeyRound, Loader2, Lock, LogOut, ShieldCheck } from 'lucide-react'
import {
  forgetSignInPassword,
  logoutUser,
  signInPassword,
  updateProfile,
  useAuthStore,
} from '../stores/authStore'
import { AuthCard, AuthError, AuthField, AuthShell } from '../components/auth/AuthShell'
import { MIN_PASSWORD_LENGTH } from '../components/profile/ChangePasswordCard'

// An admin created this account (or set its password), so they know it: the
// user picks one of their own before the app opens. The server clears
// `must_change_password` once they do (PUT /fd/auth/me).
export function ChoosePasswordPage() {
  const user = useAuthStore((s) => s.user)
  const signingOut = useAuthStore((s) => s.signingOut)
  // Just signed in with it: no need to ask again. After a reload: ask.
  const [given] = useState(signInPassword)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const temporary = given || current
  const tooShort = next.length > 0 && next.length < MIN_PASSWORD_LENGTH
  const mismatch = confirm.length > 0 && confirm !== next
  const same = next.length > 0 && next === temporary
  const valid = !!temporary && next.length >= MIN_PASSWORD_LENGTH && confirm === next && !same

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!valid || saving) return
    setSaving(true)
    setError('')
    try {
      await updateProfile({ current_password: temporary, password: next })
      forgetSignInPassword()
      // The updated user no longer has must_change_password: the app opens.
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSaving(false)
    }
  }

  return (
    <AuthShell>
      <AuthCard>
        <div className="mb-6">
          <div className="mb-5 flex h-11 w-11 items-center justify-center rounded-xl bg-violet-500/15 ring-1 ring-violet-300/25">
            <ShieldCheck className="h-5 w-5 text-violet-200" />
          </div>
          <h2 className="text-2xl font-semibold tracking-tight text-white">Choose your own password</h2>
          <p className="mt-1.5 text-sm leading-relaxed text-white/55">
            An administrator set the password for{' '}
            <span className="text-white/85">{user?.email}</span>. Pick a new one that only you know to continue.
          </p>
        </div>

        <form onSubmit={submit} className="space-y-4">
          {error && <AuthError>{error}</AuthError>}
          {/* For password managers: the account this password belongs to. */}
          <input type="text" name="username" autoComplete="username" value={user?.email ?? ''} readOnly hidden />

          {!given && (
            <AuthField
              label="Password you were given"
              icon={KeyRound}
              type="password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              autoComplete="current-password"
              required
              autoFocus
            />
          )}
          <AuthField
            label="New password"
            icon={Lock}
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
            autoComplete="new-password"
            required
            autoFocus={!!given}
            hint={
              tooShort ? <span className="text-amber-200/90">At least {MIN_PASSWORD_LENGTH} characters.</span>
                : same ? <span className="text-amber-200/90">Choose a different one from the password you were given.</span>
                  : undefined
            }
          />
          <AuthField
            label="Confirm new password"
            icon={Lock}
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            required
            hint={mismatch ? <span className="text-amber-200/90">Doesn't match.</span> : undefined}
          />

          <button
            type="submit"
            disabled={!valid || saving}
            className="fd-auth-primary mt-2 flex h-11 w-full items-center justify-center gap-2 rounded-xl text-sm font-medium text-white"
          >
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {saving ? 'Saving…' : 'Save and continue'}
            {!saving && <ArrowRight className="h-4 w-4" />}
          </button>
        </form>

        <button
          type="button"
          onClick={() => { forgetSignInPassword(); void logoutUser() }}
          disabled={signingOut}
          className="mx-auto mt-5 flex items-center gap-1.5 text-xs text-white/45 transition-colors hover:text-white/80 disabled:opacity-60"
        >
          {signingOut ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <LogOut className="h-3.5 w-3.5" />}
          {signingOut ? 'Signing out…' : `Not ${user?.display_name || user?.email}? Sign out`}
        </button>
      </AuthCard>
    </AuthShell>
  )
}
