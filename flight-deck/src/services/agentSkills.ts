/**
 * Agent skills — the "things I can do" cards of the kiosk chat
 * (`captain_claw/flight_deck/agent_skills.py`). Flight Deck writes them per
 * agent from its archetype, instructions, experience, scheduled tasks, cron
 * jobs, topics and intentions; a click sends the card's prompt to the agent.
 */

import {
  BarChart3, Bell, BookOpen, Calendar, Clock, Code2, Database, FileText, Folder, Globe,
  Image, ListChecks, Mail, MessageSquare, PenLine, Search, Sparkles, Target, User, Wallet,
  type LucideIcon,
} from 'lucide-react'
import { api } from './fileLibraryApi'

export type SkillSource = 'archetype' | 'experience' | 'schedule' | 'topic' | 'intention'

/** What a card needs from the person before the agent can start. */
export interface SkillInput {
  needs: 'none' | 'optional' | 'required'
  /** The question the input dialog asks. */
  ask: string
  accepts: ('text' | 'file' | 'image')[]
}

export interface AgentSkill {
  title: string
  description: string
  /** What a click sends, as if the person typed it (with what they add, after it). */
  prompt: string
  icon: string
  source: SkillSource
  /** Absent on cards written before cards could ask: none. */
  input?: SkillInput
}

/** Does picking this card open the input dialog first? */
export function asksForInput(skill: AgentSkill): boolean {
  return !!skill.input && skill.input.needs !== 'none'
}

export interface AgentSkills {
  /** The cards up front (kept fresh on their own). */
  skills: AgentSkill[]
  /** 'model' — written by the owner's fast tier; 'basic' — built from schedules and topics. */
  source: 'model' | 'basic'
  model: string
  generated_at: number | null
  /** New cards up front are being written; ask again shortly. */
  generating: boolean
  /** The "More" list — written only when asked for (`agentSkillsApi.more`). */
  more: AgentSkill[]
  /** When the More list was written (epoch seconds); null — never. */
  more_generated_at: number | null
  /** Why the last request for more came back with nothing. */
  more_error: string
  generating_more: boolean
}

export const SKILL_ICONS: Record<string, LucideIcon> = {
  calendar: Calendar, mail: Mail, user: User, search: Search, file: FileText, chart: BarChart3,
  clock: Clock, sparkles: Sparkles, list: ListChecks, globe: Globe, pen: PenLine, bell: Bell,
  folder: Folder, database: Database, message: MessageSquare, book: BookOpen, target: Target,
  code: Code2, image: Image, wallet: Wallet,
}

/** The cards the composer's list offers; up to 16 more are written when the
 *  "More" dialog asks for them. */
export const SKILLS_UP_FRONT = 6

/** The agent's process slug, for the agents that have cards (process agents). */
export function skillsSlug(agentId: string): string | null {
  return agentId.startsWith('proc-') ? agentId.slice(5) : null
}

export const agentSkillsApi = {
  get: (slug: string) => api<AgentSkills>(`/fd/agent-skills/${encodeURIComponent(slug)}`),
  refresh: (slug: string) =>
    api<AgentSkills>(`/fd/agent-skills/${encodeURIComponent(slug)}/refresh`, { method: 'POST' }),
  /** Write the More list now — only ever on the person's request. */
  more: (slug: string) =>
    api<AgentSkills>(`/fd/agent-skills/${encodeURIComponent(slug)}/more`, { method: 'POST' }),
}
