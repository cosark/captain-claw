import { useEffect } from 'react'
import { useAgentSkillsStore } from '../stores/agentSkillsStore'
import { skillsSlug } from '../services/agentSkills'

/** The agent's cards, fetched once per agent and kept fresh by the store.
 *  Only process agents have them (`enabled` false → none). */
export function useAgentSkills(agentId: string, enabled: boolean) {
  const slug = enabled ? skillsSlug(agentId) : null
  const entry = useAgentSkillsStore((s) => (slug ? s.bySlug[slug] : undefined))
  const load = useAgentSkillsStore((s) => s.load)
  const refreshSkills = useAgentSkillsStore((s) => s.refresh)
  const suggestMore = useAgentSkillsStore((s) => s.suggestMore)
  useEffect(() => { if (slug) load(slug) }, [slug, load])
  const data = entry?.data
  return {
    available: !!slug,
    /** An answer from Flight Deck is in hand. */
    loaded: !!data,
    skills: data?.skills ?? [],
    /** Nothing to show yet. */
    loading: !!slug && !data && !entry?.error,
    /** New cards up front are being written. */
    generating: !!data?.generating,
    /** The "More" list: written only when asked for. */
    more: data?.more ?? [],
    moreGeneratedAt: data?.more_generated_at ?? null,
    moreError: data?.more_error ?? '',
    generatingMore: !!data?.generating_more,
    reload: () => { if (slug) load(slug) },
    refresh: () => { if (slug) refreshSkills(slug) },
    suggestMore: () => { if (slug) suggestMore(slug) },
  }
}

export type SkillsState = ReturnType<typeof useAgentSkills>
