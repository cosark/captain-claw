import { Radio } from 'lucide-react'
import { useAuthStore } from '../../stores/authStore'
import { APP_VERSION, BUILD_DATE } from '../../version'

// Top-left of the side panels: the deck's logo and name (Admin → Settings →
// Branding) with the version under it. The logo carries the live-connection
// dot the radio mark shows when there is no logo.
export function BrandMark({ connected }: { connected: boolean }) {
  const name = useAuthStore((s) => s.branding.name)
  const logo = useAuthStore((s) => s.branding.logo)
  const status = connected ? 'Connected' : 'Not connected'
  return (
    <div className="flex min-w-0 items-center gap-2">
      {logo ? (
        <span className="relative shrink-0" title={status}>
          <img src={logo} alt="" className="h-7 w-7 rounded-md object-contain" />
          <span
            className={`absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full ring-2 ring-zinc-900 ${
              connected ? 'bg-emerald-500 dark:bg-emerald-400' : 'bg-zinc-500'
            }`}
          />
        </span>
      ) : (
        <Radio
          aria-label={status}
          className={`h-4 w-4 shrink-0 ${connected ? 'text-emerald-600 dark:text-emerald-400' : 'text-zinc-600'}`}
        />
      )}
      <div className="min-w-0">
        <span className="block truncate text-sm font-semibold tracking-tight" title={name}>{name}</span>
        <div className="text-[9px] leading-none text-zinc-600">v{APP_VERSION} &middot; {BUILD_DATE}</div>
      </div>
    </div>
  )
}
