import { create } from 'zustand'
import { agentSkillsApi, type AgentSkills } from '../services/agentSkills'

/** The kiosk chat's skill cards, per process agent (by slug). Flight Deck
 *  writes new cards up front in the background when the last ones are out of
 *  date, and the "More" list when asked for: while either is being written,
 *  the store asks again every few seconds (one poller per agent). */
interface Entry {
  data: AgentSkills | null
  loading: boolean
  error: string
  fetchedAt: number
}

interface AgentSkillsStore {
  bySlug: Record<string, Entry>
  /** Fetch the agent's cards unless a recent answer is in hand. */
  load: (slug: string) => void
  /** Have Flight Deck write the agent's cards up front again. */
  refresh: (slug: string) => void
  /** Have Flight Deck write the agent's More list (the person asked). */
  suggestMore: (slug: string) => void
}

const STALE_MS = 5 * 60_000
const POLL_MS = 4000
const MAX_POLLS = 75   // five minutes of a slow model

const polls = new Map<string, number>()
const timers = new Map<string, ReturnType<typeof setTimeout>>()

export const useAgentSkillsStore = create<AgentSkillsStore>((set, get) => {
  const empty: Entry = { data: null, loading: false, error: '', fetchedAt: 0 }
  const patch = (slug: string, p: Partial<Entry>) =>
    set((s) => ({ bySlug: { ...s.bySlug, [slug]: { ...(s.bySlug[slug] ?? empty), ...p } } }))

  const settle = (slug: string, data: AgentSkills) => {
    patch(slug, { data, loading: false, error: '', fetchedAt: Date.now() })
    const n = polls.get(slug) ?? 0
    if ((data.generating || data.generating_more) && n < MAX_POLLS) {
      if (!timers.has(slug)) {
        polls.set(slug, n + 1)
        timers.set(slug, setTimeout(() => { timers.delete(slug); void fetchNow(slug) }, POLL_MS))
      }
    } else {
      polls.delete(slug)
    }
  }

  const fetchNow = async (slug: string, call = agentSkillsApi.get) => {
    patch(slug, { loading: true })
    try {
      settle(slug, await call(slug))
    } catch (e) {
      polls.delete(slug)
      patch(slug, { loading: false, error: e instanceof Error ? e.message : String(e), fetchedAt: Date.now() })
    }
  }

  /** Ask Flight Deck to write (`call`), showing it as under way at once. */
  const ask = (slug: string, flag: 'generating' | 'generating_more', call: typeof agentSkillsApi.get) => {
    const data = get().bySlug[slug]?.data
    if (data?.[flag]) return
    if (data) patch(slug, { data: { ...data, [flag]: true } })
    polls.set(slug, 0)
    void fetchNow(slug, call)
  }

  return {
    bySlug: {},
    load: (slug) => {
      const e = get().bySlug[slug]
      if (e && (e.loading || timers.has(slug) || Date.now() - e.fetchedAt < STALE_MS)) return
      void fetchNow(slug)
    },
    refresh: (slug) => ask(slug, 'generating', agentSkillsApi.refresh),
    suggestMore: (slug) => ask(slug, 'generating_more', agentSkillsApi.more),
  }
})
