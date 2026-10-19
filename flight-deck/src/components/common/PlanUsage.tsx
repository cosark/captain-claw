import { useEffect } from 'react'
import { Gauge, Hourglass } from 'lucide-react'
import type { QuotaAllowance, QuotaStatus } from '../../services/profile'
import { quotaLimited, useQuotaStore } from '../../stores/quotaStore'

// ── Daily plan allowances, as the person sees them ──
//
// PlanUsageBar sits at the top of the chat, under the lane tabs: in the simple
// (kiosk) layout it is always there while the plan has daily limits; in the
// full layout only once an allowance runs low. When one is spent the composer
// is closed and the bar says why and when it comes back. PlanUsageMeters is the Info dialog's
// detailed view. "Tokens" are "AI usage" here — a kiosk user needn't know
// what a token is.

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`
  return String(n)
}

/** The reset moment in the person's own clock, e.g. "02:00". */
function resetClock(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** How long until *iso*, e.g. "5 h 12 min". */
function resetIn(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now()
  if (!Number.isFinite(ms)) return ''
  const minutes = Math.max(1, Math.round(ms / 60000))
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return h ? `${h} h ${m} min` : `${m} min`
}

function pct(a: QuotaAllowance): number {
  return a.limit > 0 ? Math.min(100, Math.round((a.used / a.limit) * 100)) : 0
}

/** Why the composer is closed, in a sentence. */
function quotaBlockedText(s: QuotaStatus): string {
  const what = s.blocked === 'tokens'
    ? "today's AI usage"
    : `today's ${s.messages.limit} message${s.messages.limit === 1 ? '' : 's'}`
  return `You've used ${what} on the ${s.plan_label} plan.`
}

/** Keeps the allowances fresh while they matter: on mount, and every minute
 *  (and on focus) while one is spent — an admin may give it back. */
function useQuotaRefresh(): QuotaStatus | null {
  const status = useQuotaStore((s) => s.status)
  const refresh = useQuotaStore((s) => s.refresh)
  useEffect(() => { void refresh() }, [refresh])
  const blocked = !!status?.blocked
  useEffect(() => {
    if (!blocked) return
    const again = () => { void refresh() }
    window.addEventListener('focus', again)
    const t = setInterval(again, 60_000)
    return () => { window.removeEventListener('focus', again); clearInterval(t) }
  }, [blocked, refresh])
  return status
}

export function PlanUsageBar({ simple }: { simple: boolean }) {
  const status = useQuotaRefresh()
  if (!quotaLimited(status)) return null
  const s = status

  if (s.blocked) {
    return (
      <div role="status" className="flex items-start gap-2.5 border-b border-red-500/30 bg-red-500/10 px-3 py-2.5 text-sm text-red-700 dark:text-red-300">
        <Hourglass className="mt-0.5 h-4 w-4 shrink-0" />
        <div className="min-w-0">
          <div className="font-medium">{quotaBlockedText(s)}</div>
          <div className="text-xs opacity-90">
            You can chat again at {resetClock(s.resets_at)} (in {resetIn(s.resets_at)}). Ask your admin if you need a bigger plan.
          </div>
        </div>
      </div>
    )
  }

  if (!simple && !s.warning) return null
  const parts: string[] = []
  if (s.messages.limit > 0) {
    const left = s.messages.remaining ?? 0
    parts.push(`${left} of ${s.messages.limit} message${s.messages.limit === 1 ? '' : 's'} left today`)
  }
  if (s.tokens.limit > 0) parts.push(`AI usage ${pct(s.tokens)}%`)
  const low = !!s.warning
  return (
    <div
      className={`flex items-center gap-2 border-b px-3 py-1.5 text-xs ${low
        ? 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400'
        : 'border-zinc-800/60 text-zinc-500'}`}
      title={`${s.plan_label} plan · ${s.messages.limit > 0 ? `${s.messages.used} of ${s.messages.limit} messages` : 'unlimited messages'} · ${s.tokens.limit > 0 ? `${fmtTokens(s.tokens.used)} of ${fmtTokens(s.tokens.limit)} AI tokens` : 'unlimited AI usage'} today`}
    >
      <Gauge className="h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 flex-1 truncate">{parts.join(' · ')}</span>
      <span className="shrink-0">Resets {resetClock(s.resets_at)}</span>
    </div>
  )
}

function Meter({ label, a, fmt }: { label: string; a: QuotaAllowance; fmt: (n: number) => string }) {
  const p = pct(a)
  const tone = p >= 100 ? 'bg-red-500' : p >= 80 ? 'bg-amber-500' : 'bg-violet-500'
  return (
    <div className="rounded-lg bg-zinc-900 px-3 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[11px] text-zinc-500">{label}</span>
        <span className="text-sm font-medium text-zinc-200">
          {a.limit > 0 ? `${fmt(a.used)} of ${fmt(a.limit)}` : `${fmt(a.used)} · unlimited`}
        </span>
      </div>
      {a.limit > 0 && (
        <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-zinc-800">
          <div className={`h-full rounded-full ${tone}`} style={{ width: `${p}%` }} />
        </div>
      )}
    </div>
  )
}

/** Today's allowances in the Info dialog. */
export function PlanUsageMeters({ initial }: { initial?: QuotaStatus }) {
  const live = useQuotaRefresh()
  const s = live ?? initial ?? null
  if (!s) return null
  return (
    <div className="mt-3">
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Today</div>
      <div className="grid grid-cols-1 gap-2 wide:grid-cols-2">
        <Meter label="Messages" a={s.messages} fmt={String} />
        <Meter label="AI tokens" a={s.tokens} fmt={fmtTokens} />
      </div>
      <p className="mt-1.5 text-[11px] text-zinc-500">
        {s.exempt
          ? "Admins aren't held to the daily limits."
          : s.blocked
            ? `${quotaBlockedText(s)} You can chat again at ${resetClock(s.resets_at)} (in ${resetIn(s.resets_at)}).`
            : `Resets every day at ${resetClock(s.resets_at)} your time (midnight UTC). A reply already under way always finishes.`}
      </p>
    </div>
  )
}
