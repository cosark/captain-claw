import type { ReactNode } from 'react'
import { AlertTriangle, Loader2, Sparkles } from 'lucide-react'
import { fmtRelative } from '../../services/schedulerApi'
import type { Facet, LibraryState } from '../../services/fileLibraryApi'

// Pieces the File library and the Data library share: the scan's progress
// line, a filter list with counts, and search-term highlighting.

export const libraryIconBtn = 'rounded-lg p-2 text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-50'

type Tone = 'violet' | 'emerald'
const TONE: Record<Tone, { icon: string; bar: string }> = {
  violet: { icon: 'text-violet-500 dark:text-violet-400', bar: 'bg-violet-500' },
  emerald: { icon: 'text-emerald-600 dark:text-emerald-400', bar: 'bg-emerald-500' },
}

/** Where a library's scan stands: looking, summarising n of m, why it
 *  stopped, or up to date. `noun` is what it holds ("file", "table"). */
export function ScanStatus({ state, noun, tone = 'violet' }: { state: LibraryState | null; noun: string; tone?: Tone }) {
  if (!state) return null
  const { job, counts } = state
  const t = TONE[tone]
  const many = (n: number) => `${n} ${noun}${n === 1 ? '' : 's'}`
  if (job.running) {
    const pct = job.total ? Math.round(((job.done + job.failed) / job.total) * 100) : 0
    return (
      <div className="flex min-w-0 items-center gap-2 text-xs text-zinc-400" title={job.current ? `Reading ${job.current}` : undefined}>
        <Loader2 className={`h-3.5 w-3.5 shrink-0 animate-spin ${t.icon}`} />
        {job.phase === 'summarizing' ? (
          <>
            <span className="shrink-0">Summarising {Math.min(job.done + job.failed + 1, job.total)} of {job.total}</span>
            <span className="h-1.5 w-20 shrink-0 overflow-hidden rounded-full bg-zinc-800">
              <span className={`block h-full rounded-full ${t.bar} transition-all`} style={{ width: `${pct}%` }} />
            </span>
          </>
        ) : <span>Looking for new {noun}s…</span>}
      </div>
    )
  }
  if (job.message) {
    return (
      <div className="flex min-w-0 items-center gap-1.5 text-xs text-amber-700 dark:text-amber-400" title={job.message}>
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" /><span className="truncate">{job.message}</span>
      </div>
    )
  }
  return (
    <div className="flex min-w-0 items-center gap-1.5 text-xs text-zinc-500">
      <Sparkles className={`h-3.5 w-3.5 shrink-0 ${t.icon}`} />
      <span className="truncate">
        {counts.total === 0 ? `No ${noun}s yet`
          : counts.pending ? `${many(counts.pending)} waiting for a summary`
            : `Up to date · ${many(counts.total)}`}
        {job.finished_at && !counts.pending ? ` · checked ${fmtRelative(Math.min(job.finished_at, Date.now() / 1000))}` : ''}
      </span>
    </div>
  )
}

/** One filter: "All" plus each value with its count. A selected value that
 *  no longer has anything still shows (at 0), so it can be cleared. */
export function FacetGroup({ title, allLabel, facets, value, onChange, dot, labelOf }: {
  title: string; allLabel: string; facets: Facet[]; value: string
  onChange: (v: string) => void; dot?: (id: string) => ReactNode
  /** The label of a selected value missing from `facets`. */
  labelOf?: (id: string) => string
}) {
  const all = facets.reduce((n, f) => n + f.count, 0)
  const rows = value && !facets.some((f) => f.id === value)
    ? [...facets, { id: value, label: labelOf?.(value) || value, count: 0 }]
    : facets
  const row = (id: string, label: string, count: number, icon?: ReactNode) => (
    <li key={id || '_all'}>
      <button
        onClick={() => onChange(id === value ? '' : id)}
        className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] transition-colors ${
          value === id ? 'bg-zinc-800 font-medium text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'}`}
      >
        {icon}
        <span className="min-w-0 flex-1 truncate">{label}</span>
        <span className="shrink-0 text-[11px] tabular-nums text-zinc-500">{count}</span>
      </button>
    </li>
  )
  return (
    <div>
      <div className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{title}</div>
      <ul className="flex flex-col gap-0.5">
        {row('', allLabel, all)}
        {rows.map((f) => row(f.id, f.label, f.count, dot?.(f.id)))}
      </ul>
    </div>
  )
}

export function Highlight({ text, terms }: { text: string; terms: string[] }) {
  if (!terms.length || !text) return <>{text}</>
  const re = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi')
  const parts = text.split(re)
  return (
    <>
      {parts.map((p, i) => (i % 2 === 1
        ? <mark key={i} className="rounded-sm bg-amber-400/30 px-0.5 text-inherit">{p}</mark>
        : <span key={i}>{p}</span>))}
    </>
  )
}
