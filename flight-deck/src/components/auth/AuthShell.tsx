import { useState, type InputHTMLAttributes, type ReactNode } from 'react'
import { AlertCircle, Eye, EyeOff, type LucideIcon } from 'lucide-react'
import { useAuthStore, DEFAULT_LOGO } from '../../stores/authStore'
import { APP_VERSION, BUILD_DATE } from '../../version'

// ── The frame of the screens before the app: sign in, sign up / waitlist,
// and choosing your own password. The deck's branding (Admin → Settings →
// Branding) on one side, the form card on the other; stacked on a phone.
// Always the night palette — see `.fd-auth` in index.css.

const CAPTAIN_SITE_URL = 'https://captain-claw.com'
const CAPTAIN_REPO_URL = 'https://github.com/kstevica/captain-claw'

/** The deck's logo, or the Captain icon when there is none. */
export function BrandLogo({ size }: { size: 'sm' | 'lg' }) {
  const logo = useAuthStore((s) => s.branding.logo)
  const box = size === 'lg' ? 'h-20 w-20 rounded-2xl' : 'h-12 w-12 rounded-xl'
  return (
    <div className={`${box} shrink-0 overflow-hidden ${logo ? 'bg-white/[0.06] p-1.5 ring-1 ring-white/10' : 'shadow-lg shadow-violet-900/40 ring-1 ring-white/10'}`}>
      <img src={logo || DEFAULT_LOGO} alt="" className="h-full w-full object-contain" />
    </div>
  )
}

export function AuthShell({ children }: { children: ReactNode }) {
  const { name, description, poweredBy } = useAuthStore((s) => s.branding)

  // "Powered by Captain" and its links: Admin → Settings → Branding can hide them.
  const link = 'text-white/55 underline-offset-2 transition-colors hover:text-white/85 hover:underline'
  const footer = (
    <p className="text-[11px] text-white/35">
      {poweredBy && (
        <>
          Powered by{' '}
          <a href={CAPTAIN_SITE_URL} target="_blank" rel="noopener noreferrer" className={link}>Captain</a>
          {' '}&middot;{' '}
          <a href={CAPTAIN_REPO_URL} target="_blank" rel="noopener noreferrer" className={link}>GitHub</a>
          {' '}&middot;{' '}
        </>
      )}
      v{APP_VERSION} &middot; {BUILD_DATE}
    </p>
  )

  return (
    <div className="fd-auth relative min-h-screen overflow-hidden">
      <div aria-hidden className="fd-auth-grid pointer-events-none absolute inset-0" />
      <div aria-hidden className="fd-auth-glow pointer-events-none absolute -left-32 top-1/4 h-[28rem] w-[28rem] rounded-full" />

      <div className="relative mx-auto grid min-h-screen w-full max-w-6xl lg:grid-cols-[1.05fr_1fr]">
        {/* Brand — beside the card on a wide screen */}
        <section className="hidden flex-col justify-between px-12 py-14 lg:flex">
          <div />
          <div className="max-w-md">
            <BrandLogo size="lg" />
            <h1 className="fd-auth-title mt-8 text-5xl font-semibold leading-[1.05] tracking-tight break-words">
              {name}
            </h1>
            {description && (
              <p className="mt-5 text-lg leading-relaxed text-white/60">{description}</p>
            )}
          </div>
          {footer}
        </section>

        {/* Form */}
        <section className="flex flex-col items-center justify-center px-4 py-10 sm:px-8">
          {/* Brand — above the card on a narrow screen */}
          <div className="mb-8 flex flex-col items-center text-center lg:hidden">
            <BrandLogo size="sm" />
            <h1 className="fd-auth-title mt-4 text-3xl font-semibold tracking-tight break-words">{name}</h1>
            {description && <p className="mt-2 max-w-sm text-sm text-white/55">{description}</p>}
          </div>
          <div className="w-full max-w-[26rem]">{children}</div>
          <div className="mt-8 lg:hidden">{footer}</div>
        </section>
      </div>
    </div>
  )
}

export function AuthCard({ children }: { children: ReactNode }) {
  return <div className="fd-auth-card rounded-2xl p-6 sm:p-8">{children}</div>
}

export function AuthError({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="flex items-start gap-2.5 rounded-xl border border-red-400/20 bg-red-500/10 px-3.5 py-2.5 text-sm text-red-200">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-300" />
      <span>{children}</span>
    </div>
  )
}

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string
  icon: LucideIcon
  hint?: ReactNode
}

/** A labelled input with a leading icon; a password one gets a show/hide eye. */
export function AuthField({ label, icon: Icon, hint, type = 'text', id, ...input }: FieldProps) {
  const [shown, setShown] = useState(false)
  const isPassword = type === 'password'
  const fieldId = id ?? `fd-auth-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`
  return (
    <div>
      <label htmlFor={fieldId} className="mb-1.5 block text-[13px] font-medium text-white/75">{label}</label>
      <div className="relative">
        <Icon className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-white/35" />
        <input
          id={fieldId}
          type={isPassword && shown ? 'text' : type}
          className={`fd-auth-input h-11 w-full rounded-xl pl-10 text-base sm:text-[15px] ${isPassword ? 'pr-11' : 'pr-3.5'}`}
          {...input}
        />
        {isPassword && (
          <button
            type="button"
            onClick={() => setShown((v) => !v)}
            aria-label={shown ? 'Hide password' : 'Show password'}
            title={shown ? 'Hide password' : 'Show password'}
            className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-lg p-2 text-white/40 transition-colors hover:bg-white/5 hover:text-white/80"
          >
            {shown ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        )}
      </div>
      {hint && <div className="mt-1.5 text-xs text-white/45">{hint}</div>}
    </div>
  )
}
