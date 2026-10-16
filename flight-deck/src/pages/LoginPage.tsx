import { useState } from 'react'
import { checkAuthStatus, joinWaitlist, loginUser, registerUser, useAuthStore } from '../stores/authStore'
import { APP_VERSION, BUILD_DATE } from '../version'

type Mode = 'login' | 'register' | 'waitlist' | 'applied'

const INPUT =
  'w-full bg-[#0a0a1a] border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500 placeholder-zinc-600'

export function LoginPage() {
  const signUp = useAuthStore((s) => s.signUp)
  const [mode, setMode] = useState<Mode>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [referrerName, setReferrerName] = useState('')
  const [referrerEmail, setReferrerEmail] = useState('')
  const [appliedCohort, setAppliedCohort] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const cohort = signUp.cohort

  const switchTo = (next: Mode) => {
    setMode(next)
    setError('')
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      if (mode === 'login') {
        await loginUser(email, password)
      } else if (mode === 'register') {
        await registerUser(email, password, displayName)
      } else if (mode === 'waitlist') {
        const applied = await joinWaitlist({
          email: email.trim(),
          password,
          display_name: displayName.trim(),
          referrer_name: referrerName.trim(),
          referrer_email: referrerEmail.trim(),
        })
        setAppliedCohort(applied || cohort)
        setMode('applied')
      }
    } catch (err: any) {
      setError(err.message || 'Something went wrong')
      if (mode === 'register') {
        // Registration closed since this page loaded: offer what the deck takes now.
        try {
          await checkAuthStatus()
          if (useAuthStore.getState().signUp.mode === 'waitlist') setMode('waitlist')
        } catch { /* keep the error */ }
      }
    } finally {
      setLoading(false)
    }
  }

  const title =
    mode === 'login' ? 'Sign In' : mode === 'register' ? 'Create Account' : 'Join the Waitlist'
  const submitLabel =
    mode === 'login' ? 'Sign In' : mode === 'register' ? 'Create Account' : cohort ? `Apply for ${cohort}` : 'Join the waitlist'

  return (
    <div className="min-h-screen bg-[#0a0a1a] flex items-center justify-center px-4 py-8">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <h1 className="text-2xl font-bold text-white tracking-tight">Flight Deck</h1>
          <p className="text-zinc-500 text-sm mt-1">Agent Management Console</p>
          <p className="text-zinc-600 text-xs mt-1">v{APP_VERSION} &middot; {BUILD_DATE}</p>
        </div>

        {mode === 'applied' ? (
          <div className="bg-[#12122a] border border-zinc-800 rounded-lg p-6 space-y-4">
            <h2 className="text-lg font-semibold text-white">You're on the waitlist</h2>
            <p className="text-sm text-zinc-300">
              Thanks{displayName.trim() ? `, ${displayName.trim()}` : ''}. Your application
              {appliedCohort ? <> for <span className="text-white font-medium">{appliedCohort}</span></> : null} is in.
            </p>
            <p className="text-sm text-zinc-400">
              An administrator will review it. Once you're approved, sign in with{' '}
              <span className="text-zinc-200">{email.trim()}</span> and the password you chose.
            </p>
            <button
              type="button"
              onClick={() => { setPassword(''); switchTo('login') }}
              className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-medium py-2 rounded text-sm transition-colors"
            >
              Back to sign in
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="bg-[#12122a] border border-zinc-800 rounded-lg p-6 space-y-4">
            <div>
              <h2 className="text-lg font-semibold text-white">{title}</h2>
              {mode === 'waitlist' && (
                <p className="text-xs text-zinc-400 mt-1.5">
                  Registration is closed.
                  {cohort ? <> Apply for <span className="text-indigo-300 font-medium">{cohort}</span> — </> : ' '}
                  an administrator reviews each application, and you can sign in once you're approved.
                </p>
              )}
            </div>

            {error && (
              <div className="text-red-400 text-sm bg-red-950/30 border border-red-900/50 rounded px-3 py-2">
                {error}
              </div>
            )}

            {(mode === 'register' || mode === 'waitlist') && (
              <div>
                <label className="block text-sm text-zinc-300 mb-1">
                  {mode === 'waitlist' ? 'Your Name' : 'Display Name'}
                </label>
                <input
                  type="text"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  className={INPUT}
                  placeholder="Your name"
                  required={mode === 'waitlist'}
                  maxLength={100}
                />
              </div>
            )}

            <div>
              <label className="block text-sm text-zinc-300 mb-1">Email</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={INPUT}
                placeholder="you@example.com"
                required
                autoFocus
              />
            </div>

            <div>
              <label className="block text-sm text-zinc-300 mb-1">Password</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={INPUT}
                placeholder="At least 6 characters"
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                required
                minLength={6}
              />
              {mode === 'waitlist' && (
                <p className="text-xs text-zinc-500 mt-1">You'll sign in with this once you're approved.</p>
              )}
            </div>

            {mode === 'waitlist' && (
              <fieldset className="space-y-3 border-t border-zinc-800 pt-4">
                <legend className="sr-only">Who recommended you</legend>
                <div>
                  <p className="text-sm font-medium text-zinc-200">Who recommended you?</p>
                  <p className="text-xs text-zinc-500 mt-0.5">
                    {cohort ? `${cohort} is` : 'Applications are'} by recommendation — name the person who referred you.
                  </p>
                </div>
                <div>
                  <label className="block text-sm text-zinc-300 mb-1">Their Name</label>
                  <input
                    type="text"
                    value={referrerName}
                    onChange={(e) => setReferrerName(e.target.value)}
                    className={INPUT}
                    placeholder="Who recommended you"
                    required
                    maxLength={100}
                  />
                </div>
                <div>
                  <label className="block text-sm text-zinc-300 mb-1">Their Email</label>
                  <input
                    type="email"
                    value={referrerEmail}
                    onChange={(e) => setReferrerEmail(e.target.value)}
                    className={INPUT}
                    placeholder="them@example.com"
                    required
                  />
                </div>
              </fieldset>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-indigo-600 hover:bg-indigo-500 disabled:bg-indigo-800 disabled:cursor-not-allowed text-white font-medium py-2 rounded text-sm transition-colors"
            >
              {loading ? 'Please wait...' : submitLabel}
            </button>

            <div className="text-center text-sm text-zinc-400">
              {mode === 'login' ? (
                signUp.mode === 'closed' ? (
                  <>No account? Ask an administrator to create one.</>
                ) : (
                  <>
                    No account?{' '}
                    <button
                      type="button"
                      onClick={() => switchTo(signUp.mode === 'waitlist' ? 'waitlist' : 'register')}
                      className="text-indigo-400 hover:text-indigo-300"
                    >
                      {signUp.mode === 'waitlist' ? 'Join the waitlist' : 'Create one'}
                    </button>
                  </>
                )
              ) : (
                <>
                  Already have an account?{' '}
                  <button type="button" onClick={() => switchTo('login')} className="text-indigo-400 hover:text-indigo-300">
                    Sign in
                  </button>
                </>
              )}
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
