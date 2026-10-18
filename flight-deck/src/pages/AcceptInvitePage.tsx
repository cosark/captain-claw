import { useEffect, useState } from 'react'
import { ArrowRight, Loader2, Lock, LogOut, Mail, MailOpen, User as UserIcon } from 'lucide-react'
import {
  acceptInvite,
  logoutUser,
  lookupInvite,
  useAuthStore,
  type InviteDetails,
} from '../stores/authStore'
import { AuthCard, AuthError, AuthField, AuthShell } from '../components/auth/AuthShell'
import { MIN_PASSWORD_LENGTH } from '../components/profile/ChangePasswordCard'

// <deck>/invite#<token>: the link in an invitation email (Admin → Users →
// Invite by email). The invitee chooses their password here and the account
// is created then — they're signed in straight away. The token rides in the
// fragment, so it never reaches a server log.

export const INVITE_PATH = '/invite'

function inviteToken(): string {
  try { return decodeURIComponent(window.location.hash.replace(/^#/, '')).trim() } catch { return '' }
}

function leaveInvitePage() {
  window.history.replaceState(null, '', '/')
}

function longDate(iso: string): string {
  try { return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' }) } catch { return iso }
}

export function AcceptInvitePage({ onLeave }: { onLeave: () => void }) {
  const signedIn = useAuthStore((s) => s.isAuthenticated)
  const currentUser = useAuthStore((s) => s.user)
  const signingOut = useAuthStore((s) => s.signingOut)
  const instanceName = useAuthStore((s) => s.branding.name)
  const [token] = useState(inviteToken)
  const [invite, setInvite] = useState<InviteDetails | null>(null)
  const [lookupError, setLookupError] = useState('')
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!token) {
      setLookupError("This link is missing its invitation code. Open the link from your invitation email again.")
      return
    }
    let cancelled = false
    lookupInvite(token)
      .then((d) => { if (!cancelled) { setInvite(d); setName(d.display_name) } })
      .catch((e) => { if (!cancelled) setLookupError(e instanceof Error ? e.message : String(e)) })
    return () => { cancelled = true }
  }, [token])

  const goToApp = () => { leaveInvitePage(); onLeave() }

  const tooShort = password.length > 0 && password.length < MIN_PASSWORD_LENGTH
  const mismatch = confirm.length > 0 && confirm !== password
  const valid = password.length >= MIN_PASSWORD_LENGTH && confirm === password

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!valid || saving) return
    setSaving(true)
    setError('')
    try {
      // Off /invite first: once signed in, App renders the app at "/".
      leaveInvitePage()
      await acceptInvite(token, password, name.trim())
      onLeave()
    } catch (err) {
      window.history.replaceState(null, '', `${INVITE_PATH}#${token}`)
      setError(err instanceof Error ? err.message : String(err))
      setSaving(false)
    }
  }

  const heading = (
    <div className="mb-6">
      <div className="mb-5 flex h-11 w-11 items-center justify-center rounded-xl bg-violet-500/15 ring-1 ring-violet-300/25">
        <MailOpen className="h-5 w-5 text-violet-200" />
      </div>
      <h2 className="text-2xl font-semibold tracking-tight text-white">You're invited</h2>
      {invite ? (
        <p className="mt-1.5 text-sm leading-relaxed text-white/55">
          {invite.inviter ? <span className="text-white/85">{invite.inviter}</span> : 'An administrator'} invited you
          to join {instanceName}. Choose your password to get started.
        </p>
      ) : (
        <p className="mt-1.5 text-sm leading-relaxed text-white/55">Join {instanceName}.</p>
      )}
    </div>
  )

  // Someone is signed in in this browser: accepting would swap their session.
  if (signedIn && !saving) {
    return (
      <AuthShell>
        <AuthCard>
          {heading}
          <p className="text-sm leading-relaxed text-white/60">
            You're signed in as <span className="text-white/85">{currentUser?.email}</span>. To accept this
            invitation, sign out first — then open the link again if it doesn't come back on its own.
          </p>
          <div className="mt-6 space-y-3">
            <button
              type="button"
              onClick={() => { void logoutUser() }}
              disabled={signingOut}
              className="fd-auth-primary flex h-11 w-full items-center justify-center gap-2 rounded-xl text-sm font-medium text-white"
            >
              {signingOut ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogOut className="h-4 w-4" />}
              {signingOut ? 'Signing out…' : 'Sign out'}
            </button>
            <button
              type="button"
              onClick={goToApp}
              className="mx-auto flex items-center gap-1.5 text-xs text-white/45 transition-colors hover:text-white/80"
            >
              Continue as {currentUser?.display_name || currentUser?.email}
            </button>
          </div>
        </AuthCard>
      </AuthShell>
    )
  }

  return (
    <AuthShell>
      <AuthCard>
        {heading}

        {!invite && !lookupError && (
          <div className="flex justify-center py-6">
            <Loader2 className="h-5 w-5 animate-spin text-white/40" />
          </div>
        )}

        {lookupError && (
          <div className="space-y-5">
            <AuthError>{lookupError}</AuthError>
            <button
              type="button"
              onClick={goToApp}
              className="fd-auth-primary flex h-11 w-full items-center justify-center gap-2 rounded-xl text-sm font-medium text-white"
            >
              Go to sign in <ArrowRight className="h-4 w-4" />
            </button>
          </div>
        )}

        {invite && (
          <form onSubmit={submit} className="space-y-4">
            {error && <AuthError>{error}</AuthError>}
            <AuthField
              label="Email"
              icon={Mail}
              type="email"
              name="username"
              value={invite.email}
              autoComplete="username"
              readOnly
              hint="You'll sign in with this address."
            />
            <AuthField
              label="Your name"
              icon={UserIcon}
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="name"
              maxLength={100}
              placeholder={invite.email.split('@')[0]}
            />
            <AuthField
              label="Password"
              icon={Lock}
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
              autoComplete="new-password"
              required
              autoFocus
              hint={tooShort ? <span className="text-amber-200/90">At least {MIN_PASSWORD_LENGTH} characters.</span> : undefined}
            />
            <AuthField
              label="Confirm password"
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
              {saving ? 'Creating your account…' : 'Create account and continue'}
              {!saving && <ArrowRight className="h-4 w-4" />}
            </button>
            <p className="text-center text-[11px] text-white/35">
              This link works once and expires on {longDate(invite.expires_at)}.
            </p>
          </form>
        )}
      </AuthCard>
    </AuthShell>
  )
}
