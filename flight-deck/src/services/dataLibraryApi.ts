/**
 * Data library — the kiosk's "Data": every datastore table the caller's
 * agents keep, profiled and summarised by Flight Deck
 * (`captain_claw/flight_deck/data_library.py`). The rows are read live from
 * the agent's datastore, so the table view is never stale.
 */

import { useAuthStore } from '../stores/authStore'
import { api, type Facet, type LibraryState, type LibraryStatus } from './fileLibraryApi'

export interface DataColumn { name: string; type: string }

export type ColumnKind = 'text' | 'number' | 'url' | 'email' | 'date' | 'boolean' | 'json'

export interface ColumnProfile {
  name: string
  type: string
  filled: number
  distinct: number
  kind: ColumnKind
  min?: number | string | null
  max?: number | string | null
  avg?: number | null
  sum?: number | null
  true?: number
  false?: number
  avg_length?: number | null
  /** Values that only stand in for a missing one ("unknown", "TBD", "n/a"…) — not counted as filled. */
  placeholders?: number
  /** The most common values — only when something repeats. */
  top?: { value: string; count: number }[]
}

export interface DataTable {
  id: string
  agent_slug: string
  agent_name: string
  agent_running: boolean
  table_name: string
  columns: DataColumn[]
  row_count: number
  /** ISO time of the table's last change. */
  updated_at: string
  status: LibraryStatus
  title: string
  summary: string
  category: string
  tags: string[]
  insights: string[]
  /** What each column holds, in a line. */
  column_notes: Record<string, string>
  error: string
  model: string
  summarized_at: number | null
  /** Row text around a search term the card doesn't show. */
  snippet: string
  /** Only from `table(id)`. */
  profile?: ColumnProfile[]
}

export interface DataPage {
  items: DataTable[]
  total: number
  rows_total: number
  facets: { agents: Facet[]; categories: Facet[] }
}

export type DataRow = Record<string, unknown> & { __rowid: number }

export interface RowsPage {
  columns: DataColumn[]
  rows: DataRow[]
  total: number
}

export type DataSort = 'recent' | 'name' | 'rows' | 'agent'

export interface DataQuery {
  q?: string
  agent?: string
  category?: string
  sort?: DataSort
  limit?: number
  offset?: number
}

function qs(params: Record<string, unknown>): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') p.set(k, String(v))
  return p.toString()
}

export const dataLibraryApi = {
  status: () => api<LibraryState>('/fd/data-library/status'),
  /** Look for new and changed tables; `retryErrors` also retries failed summaries now. */
  scan: (retryErrors = false) =>
    api<LibraryState>(`/fd/data-library/scan${retryErrors ? '?retry_errors=true' : ''}`, { method: 'POST' }),
  tables: (query: DataQuery = {}) => api<DataPage>(`/fd/data-library/tables?${qs({ ...query })}`),
  table: (id: string) => api<DataTable>(`/fd/data-library/tables/${encodeURIComponent(id)}`),
  rows: (id: string, opts: { limit?: number; offset?: number; q?: string; sort?: string; dir?: 'asc' | 'desc' }) =>
    api<RowsPage>(`/fd/data-library/tables/${encodeURIComponent(id)}/rows?${qs(opts)}`),
  resummarize: (id: string) =>
    api<DataTable>(`/fd/data-library/tables/${encodeURIComponent(id)}/summarize`, { method: 'POST' }),
  /** The whole table as CSV; the FD token rides along for a plain link. */
  exportUrl: (id: string) => {
    const { token, authEnabled } = useAuthStore.getState()
    const t = authEnabled && token ? `?fd_token=${encodeURIComponent(token)}` : ''
    return `/fd/data-library/tables/${encodeURIComponent(id)}/export${t}`
  },
}
