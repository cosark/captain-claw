import type { ReactNode } from 'react'
import { Search, X } from 'lucide-react'

// The search box, status switch and filter menus over the Users page's lists
// (Users, Invitations, Waitlist). Filtering happens in the list itself
// (matchesQuery in deckConfig).

export function SearchBox({ value, onChange, placeholder }: {
  value: string
  onChange: (v: string) => void
  placeholder: string
}) {
  return (
    <div className="relative min-w-0 flex-1">
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Escape' && value) { e.preventDefault(); onChange('') } }}
        placeholder={placeholder}
        aria-label={placeholder}
        className="w-full rounded-lg border border-zinc-800 bg-zinc-900 py-1.5 pl-8 pr-8 text-sm text-zinc-200 placeholder-zinc-500 focus:border-violet-500/60 focus:outline-none [&::-webkit-search-cancel-button]:hidden"
      />
      {value && (
        <button
          onClick={() => onChange('')}
          className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-zinc-500 hover:text-zinc-200"
          aria-label="Clear search"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  )
}

export interface SegmentOption<T extends string> {
  value: T
  label: string
  count?: number
  // Draws the count in amber: something here wants a look.
  warn?: boolean
}

export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T
  options: SegmentOption<T>[]
  onChange: (v: T) => void
  label: string
}) {
  return (
    <div className="inline-flex flex-wrap rounded-md border border-zinc-700 p-0.5" role="radiogroup" aria-label={label}>
      {options.map((o) => {
        const on = value === o.value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(o.value)}
            className={`flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium transition-colors ${
              on ? 'bg-violet-600 text-white' : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200'
            }`}
          >
            {o.label}
            {o.count !== undefined && (
              <span className={`tabular-nums ${
                on ? 'text-violet-200' : o.warn && o.count > 0 ? 'text-amber-500' : 'text-zinc-500'
              }`}>
                {o.count}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

export function FilterSelect({ value, onChange, label, active, children }: {
  value: string
  onChange: (v: string) => void
  label: string
  // Not at its "everything" choice: drawn so it stands out.
  active: boolean
  children: ReactNode
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      title={label}
      className={`rounded-md border bg-zinc-950 px-2 py-1 text-xs focus:border-violet-500/50 focus:outline-none ${
        active ? 'border-violet-600/60 text-zinc-100' : 'border-zinc-700 text-zinc-400'
      }`}
    >
      {children}
    </select>
  )
}

// "3 of 12 users" under a filtered list, with a way back to all of them.
export function FilterSummary({ shown, total, noun, onClear }: {
  shown: number
  total: number
  noun: string
  onClear: () => void
}) {
  return (
    <div className="flex items-center gap-2 px-1 text-xs text-zinc-500">
      <span>{shown} of {total} {noun}</span>
      <button onClick={onClear} className="text-violet-400 underline underline-offset-2 hover:no-underline">
        Clear filters
      </button>
    </div>
  )
}
