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
  useEffect(() => { if (slug) load(slug) }, [slug, load])
  return {
    available: !!slug,
    skills: entry?.data?.skills ?? [],
    /** Nothing to show yet. */
    loading: !!slug && !entry?.data && !entry?.error,
    /** A better set is being written. */
    generating: !!entry?.data?.generating,
    reload: () => { if (slug) load(slug) },
    refresh: () => { if (slug) refreshSkills(slug) },
  }
}

export type SkillsState = ReturnType<typeof useAgentSkills>
