import { useEffect, useRef } from 'react'
import { ChevronDown, ChevronRight, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import { useAuthStore } from '../../stores/authStore'
import { SKILL_ICONS, SKILLS_UP_FRONT, type AgentSkill } from '../../services/agentSkills'
import type { SkillsState } from '../../hooks/useAgentSkills'
import { AgentDialog } from '../layout/KioskAgentDialogs'

// The kiosk chat's "things I can do": a greeting with the agent's top cards
// on an empty chat (or a fresh topic), the cards up front behind the
// composer's "What can I do?" button, and its "More" dialog — the only place
// the More list is asked for. A card's click sends its prompt.

const ICON_SIZE = {
  sm: ['h-8 w-8 rounded-lg', 'h-4 w-4'],
  md: ['h-10 w-10 rounded-xl', 'h-5 w-5'],
  lg: ['h-11 w-11 rounded-xl wide:h-12 wide:w-12', 'h-5 w-5 wide:h-6 wide:w-6'],
} as const

function SkillIcon({ name, size }: { name: string; size: keyof typeof ICON_SIZE }) {
  const Icon = SKILL_ICONS[name] || Sparkles
  const [box, glyph] = ICON_SIZE[size]
  return (
    <span className={`flex shrink-0 items-center justify-center bg-violet-500/10 text-violet-600 dark:text-violet-300 ${box}`}>
      <Icon className={glyph} strokeWidth={1.75} />
    </span>
  )
}

/** Write the cards up front again. */
function RefreshButton({ skills }: { skills: SkillsState }) {
  return (
    <button
      type="button"
      onClick={skills.refresh}
      disabled={skills.generating}
      title="Suggest these again from what the agent knows now"
      className="ml-auto rounded-md p-1.5 text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-300 disabled:opacity-60"
    >
      <RefreshCw className={`h-4 w-4 ${skills.generating ? 'animate-spin' : ''}`} />
    </button>
  )
}

function startOfDay(daysAgo = 0): number {
  const d = new Date()
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - daysAgo).getTime()
}

/** "today", "yesterday" or "on 18 Oct" — when the More list was written. */
function writtenWhen(epochSeconds: number): string {
  const t = epochSeconds * 1000
  if (t >= startOfDay()) return 'today'
  if (t >= startOfDay(1)) return 'yesterday'
  return `on ${new Date(t).toLocaleDateString([], { day: 'numeric', month: 'short' })}`
}

function greeting(now = new Date()): string {
  const h = now.getHours()
  return h >= 5 && h < 12 ? 'Good morning' : h >= 12 && h < 18 ? 'Good afternoon' : 'Good evening'
}

const HERO_CARDS = 3

/** The empty chat: "Good afternoon, Ana." and the agent's top cards. */
export function ChatGreeting({ skills, disabled, onPick, onMore }: {
  skills: SkillsState
  disabled: boolean
  onPick: (skill: AgentSkill) => void
  onMore: () => void
}) {
  const name = useAuthStore((s) => s.user?.display_name?.trim().split(/\s+/)[0] || '')
  const shown = skills.skills.slice(0, HERO_CARDS)
  return (
    <div className="pb-6 pt-8 wide:pt-14">
      <h1 className="text-[2.1rem] font-semibold leading-[1.1] tracking-[-0.03em] text-zinc-100 wide:text-5xl">
        {greeting()}{name ? `, ${name}` : ''}.
      </h1>
      <p className="mt-2 text-base text-zinc-400 wide:text-lg">What would you like to work on?</p>

      {skills.available && (shown.length > 0 || skills.loading) && (
        <>
          <div className="mb-3 mt-8 flex items-center gap-2 text-[15px] text-zinc-400">
            <span>Here are a few things I can do</span>
            {skills.generating && <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-500" aria-label="Updating" />}
          </div>
          <div className="space-y-3">
            {shown.length === 0 && Array.from({ length: HERO_CARDS }, (_, i) => (
              <div key={i} className="h-[84px] animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/40" />
            ))}
            {shown.map((s) => (
              <button
                key={s.title}
                type="button"
                disabled={disabled}
                onClick={() => onPick(s)}
                title={s.prompt}
                className="flex w-full items-center gap-4 rounded-2xl border border-zinc-800 bg-zinc-900/60 px-4 py-3.5 text-left shadow-sm transition-colors hover:border-violet-500/40 hover:bg-zinc-900 disabled:cursor-not-allowed disabled:opacity-50 wide:px-5 wide:py-4"
              >
                <SkillIcon name={s.icon} size="lg" />
                <span className="min-w-0">
                  <span className="block text-[1.05rem] font-semibold leading-snug tracking-tight text-zinc-100 wide:text-lg">{s.title}</span>
                  <span className="mt-0.5 block text-sm text-zinc-400 wide:text-[15px]">{s.description}</span>
                </span>
              </button>
            ))}
          </div>
          {shown.length > 0 && (
            <button
              type="button"
              onClick={onMore}
              className="mt-3 rounded-md px-1 text-sm text-zinc-500 transition-colors hover:text-zinc-300"
            >
              More things I can do…
            </button>
          )}
        </>
      )}
    </div>
  )
}

/** The composer's "What can I do?" button and its list. The list opens above
 *  the composer: the nearest positioned ancestor places it. */
export function SkillsMenu({ skills, open, setOpen, disabled, onPick, onMore, compact }: {
  skills: SkillsState
  open: boolean
  setOpen: (open: boolean) => void
  disabled: boolean
  onPick: (skill: AgentSkill) => void
  /** Open the dialog with every card. */
  onMore: () => void
  /** Phone: the button shows its icon only. */
  compact?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    skills.reload()
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!ref.current?.contains(t) && !buttonRef.current?.contains(t)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  if (!skills.available) return null
  const more = skills.more.length
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        title="Things this agent can do for you"
        className={`flex h-9 shrink-0 items-center gap-1.5 rounded-lg border px-2.5 text-sm transition-colors ${
          open
            ? 'border-violet-500/40 bg-violet-500/10 text-zinc-100'
            : 'border-zinc-700 text-zinc-300 hover:bg-zinc-800'
        }`}
      >
        <Sparkles className="h-4 w-4 text-violet-500" />
        {!compact && <span>What can I do?</span>}
        <ChevronDown className={`h-3.5 w-3.5 text-zinc-500 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div
          ref={ref}
          className="absolute inset-x-0 bottom-full z-40 mb-2 flex max-h-[min(60vh,32rem)] flex-col overflow-hidden rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        >
          <div className="flex items-center gap-2 border-b border-zinc-800 px-4 py-2.5">
            <span className="text-sm font-medium text-zinc-200">Things I can do</span>
            {skills.generating && <span className="text-xs text-zinc-500">· updating…</span>}
            <RefreshButton skills={skills} />
          </div>
          <div className="overflow-y-auto p-1.5">
            {skills.skills.length === 0 && (
              <div className="flex items-center gap-2 px-3 py-4 text-sm text-zinc-500">
                {skills.loading && <Loader2 className="h-4 w-4 animate-spin" />}
                {skills.loading ? 'Looking at what I can do…' : 'Nothing to suggest yet.'}
              </div>
            )}
            {skills.skills.slice(0, SKILLS_UP_FRONT).map((s) => (
              <button
                key={s.title}
                type="button"
                disabled={disabled}
                onClick={() => { setOpen(false); onPick(s) }}
                title={s.prompt}
                className="flex w-full items-center gap-3 rounded-xl px-2.5 py-2 text-left transition-colors hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <SkillIcon name={s.icon} size="sm" />
                <span className="min-w-0">
                  <span className="block truncate text-[15px] font-medium text-zinc-100">{s.title}</span>
                  <span className="block truncate text-sm text-zinc-400">{s.description}</span>
                </span>
              </button>
            ))}
          </div>
          {skills.loaded && (
            <button
              type="button"
              onClick={() => { setOpen(false); onMore() }}
              className="flex shrink-0 items-center justify-center gap-1 border-t border-zinc-800 px-4 py-2.5 text-sm font-medium text-violet-600 transition-colors hover:bg-zinc-800 dark:text-violet-300"
            >
              More
              {more > 0 && <span className="font-normal text-zinc-500">· {more}</span>}
              <ChevronRight className="h-4 w-4" />
            </button>
          )}
        </div>
      )}
    </>
  )
}

function SkillCard({ skill, disabled, onPick }: { skill: AgentSkill; disabled: boolean; onPick: (skill: AgentSkill) => void }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onPick(skill)}
      title={skill.prompt}
      className="flex w-full items-start gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/60 px-4 py-3 text-left transition-colors hover:border-violet-500/40 hover:bg-zinc-900 disabled:cursor-not-allowed disabled:opacity-50"
    >
      <SkillIcon name={skill.icon} size="md" />
      <span className="min-w-0 pt-0.5">
        <span className="block text-[15px] font-semibold leading-snug tracking-tight text-zinc-100">{skill.title}</span>
        <span className="mt-0.5 line-clamp-2 block text-sm text-zinc-400">{skill.description}</span>
      </span>
    </button>
  )
}

/** Every card the agent has: the ones offered up front, then the More list.
 *  Opening it with no More list asks for one (the person asked by opening
 *  it); one written on an earlier day is offered to be written again — never
 *  rewritten on its own. */
export function SkillsDialog({ skills, agentName, disabled, onPick, onClose }: {
  skills: SkillsState
  agentName: string
  disabled: boolean
  onPick: (skill: AgentSkill) => void
  onClose: () => void
}) {
  const pick = (s: AgentSkill) => { onClose(); onPick(s) }
  const asked = useRef(false)
  const wantMore = skills.loaded && skills.more.length === 0 && !skills.generatingMore
  useEffect(() => {
    if (!wantMore || asked.current) return
    asked.current = true
    skills.suggestMore()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantMore])

  const stale = !!skills.moreGeneratedAt && skills.moreGeneratedAt * 1000 < startOfDay()
  const grid = (cards: AgentSkill[]) => (
    <div className="grid grid-cols-1 gap-2.5 wide:grid-cols-2">
      {cards.map((s) => <SkillCard key={s.title} skill={s} disabled={disabled} onPick={pick} />)}
    </div>
  )
  return (
    <AgentDialog title="Things I can do" icon={Sparkles} agentName={agentName} size="lg" onClose={onClose}>
      <div className="shrink-0 border-b border-zinc-800 px-5 py-2 text-sm text-zinc-500">
        {disabled ? 'Ready when the current reply finishes.' : 'Pick one and I start on it right away.'}
      </div>
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <section className="mb-6">
          <div className="mb-2.5 flex items-center gap-2">
            <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-500">Suggested</h3>
            {skills.generating && <span className="text-xs text-zinc-500">· updating…</span>}
            <RefreshButton skills={skills} />
          </div>
          {skills.skills.length === 0 ? (
            <div className="flex items-center gap-2 py-4 text-sm text-zinc-500">
              {skills.loading && <Loader2 className="h-4 w-4 animate-spin" />}
              {skills.loading ? 'Looking at what I can do…' : 'Nothing to suggest yet.'}
            </div>
          ) : grid(skills.skills.slice(0, SKILLS_UP_FRONT))}
        </section>

        <section>
          <div className="mb-2.5 flex items-center gap-2">
            <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-500">More</h3>
            {skills.moreGeneratedAt && !skills.generatingMore && (
              <span className="text-xs text-zinc-500">· suggested {writtenWhen(skills.moreGeneratedAt)}</span>
            )}
            {skills.more.length > 0 && (
              <button
                type="button"
                onClick={skills.suggestMore}
                disabled={skills.generatingMore}
                className="ml-auto flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-300 disabled:opacity-60"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${skills.generatingMore ? 'animate-spin' : ''}`} />
                {skills.generatingMore ? 'Suggesting…' : 'Suggest again'}
              </button>
            )}
          </div>
          {stale && !skills.generatingMore && (
            <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-violet-500/30 bg-violet-500/10 px-3.5 py-2.5 text-sm text-zinc-300">
              <span className="min-w-0 flex-1">These are from {writtenWhen(skills.moreGeneratedAt!)}. Want new ones from what I know now?</span>
              <button
                type="button"
                onClick={skills.suggestMore}
                className="shrink-0 rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500"
              >
                Suggest new ones
              </button>
            </div>
          )}
          {skills.more.length > 0 ? grid(skills.more) : skills.generatingMore || !skills.loaded ? (
            <>
              <div className="mb-3 flex items-center gap-2 text-sm text-zinc-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                Finding more things I can do…
              </div>
              <div className="grid grid-cols-1 gap-2.5 wide:grid-cols-2">
                {Array.from({ length: 4 }, (_, i) => (
                  <div key={i} className="h-[74px] animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/40" />
                ))}
              </div>
            </>
          ) : (
            <div className="flex flex-wrap items-center gap-3 py-2 text-sm text-zinc-500">
              <span>{skills.moreError ? "I couldn't come up with more right now." : 'Nothing more to suggest yet.'}</span>
              <button
                type="button"
                onClick={skills.suggestMore}
                className="rounded-md px-2 py-1 text-xs font-medium text-violet-600 hover:bg-zinc-800 dark:text-violet-300"
              >
                Try again
              </button>
            </div>
          )}
        </section>
      </div>
    </AgentDialog>
  )
}
