import { create } from 'zustand'
import { agentSkillsApi, type AgentSkills } from '../services/agentSkills'

/** The kiosk chat's skill cards, per process agent (by slug). Flight Deck
 *  writes a new set in the background when the last one is out of date: while
 *  it does (`generating`), the store asks again every few seconds. */
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
  /** Have Flight Deck write the agent's cards again. */
  refresh: (slug: string) => void
}

const STALE_MS = 5 * 60_000
const POLL_MS = 4000
const MAX_POLLS = 30   // two minutes of a slow model

const polls = new Map<string, number>()

export const useAgentSkillsStore = create<AgentSkillsStore>((set, get) => {
  const empty: Entry = { data: null, loading: false, error: '', fetchedAt: 0 }
  const patch = (slug: string, p: Partial<Entry>) =>
    set((s) => ({ bySlug: { ...s.bySlug, [slug]: { ...(s.bySlug[slug] ?? empty), ...p } } }))

  const settle = (slug: string, data: AgentSkills) => {
    patch(slug, { data, loading: false, error: '', fetchedAt: Date.now() })
    const n = polls.get(slug) ?? 0
    if (data.generating && n < MAX_POLLS) {
      polls.set(slug, n + 1)
      setTimeout(() => { void fetchNow(slug) }, POLL_MS)
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

  return {
    bySlug: {},
    load: (slug) => {
      const e = get().bySlug[slug]
      if (e && (e.loading || polls.has(slug) || Date.now() - e.fetchedAt < STALE_MS)) return
      void fetchNow(slug)
    },
    refresh: (slug) => {
      if (polls.has(slug)) return
      polls.set(slug, 0)
      void fetchNow(slug, agentSkillsApi.refresh)
    },
  }
})
