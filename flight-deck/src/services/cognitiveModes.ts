// The cognitive modes an agent can think in (captain_claw/cognitive_mode.py),
// with how Flight Deck shows them — the agent cards' selector and the kiosk's
// agent Settings.

export interface ModeInfo {
  id: string
  label: string
  character: string
  description: string
  color: string      // tailwind bg class for the dot
  textColor: string  // tailwind text class for active state
  bgActive: string   // tailwind bg class for active row
}

export const MODES: ModeInfo[] = [
  { id: 'neutra',     label: 'Neutra',     character: 'Balanced generalist',
    description: 'No cognitive bias in any direction. The default mode — responds to cognitive tempo as the primary driver. Equal weight to analysis, creativity, speed, and caution.',
    color: 'bg-zinc-500',    textColor: 'text-zinc-400',    bgActive: 'bg-zinc-500/10' },
  { id: 'ionian',     label: 'Ionian',     character: 'Convergent problem-solving',
    description: 'Drives toward clear answers and closure. Defines the goal state upfront, maps known solutions, picks the most direct path, and executes with confidence. Assertive, linear, minimal hedging.',
    color: 'bg-amber-400',   textColor: 'text-amber-400',   bgActive: 'bg-amber-500/10' },
  { id: 'dorian',     label: 'Dorian',     character: 'Empathetic pragmatism',
    description: 'Acknowledges the full context — technical constraints, team dynamics, time pressure — and finds the best realistic path forward. Honest about tradeoffs, avoids both perfectionism and cynicism. Leaves breadcrumbs for future decisions.',
    color: 'bg-teal-400',    textColor: 'text-teal-400',    bgActive: 'bg-teal-500/10' },
  { id: 'phrygian',   label: 'Phrygian',   character: 'Adversarial analysis',
    description: 'Assumes hostile conditions. Probes edge cases, finds the weakest link, stress-tests every decision. Skeptical by default — never says "it should work" without qualifying the conditions. Generates more questions than answers initially.',
    color: 'bg-red-400',     textColor: 'text-red-400',     bgActive: 'bg-red-500/10' },
  { id: 'lydian',     label: 'Lydian',     character: 'Creative exploration',
    description: 'Suspends constraints to explore what is possible before deciding what is feasible. Reframes problems from unexpected angles, generates multiple divergent approaches, and finds cross-domain connections. Comfortable with ambiguity.',
    color: 'bg-violet-400',  textColor: 'text-violet-400',  bgActive: 'bg-violet-500/10' },
  { id: 'mixolydian', label: 'Mixolydian', character: 'Iterative building',
    description: 'Gets the smallest viable thing working first, then learns and improves. Action-biased and prototype-oriented — a rough working version beats a polished spec. Ships at every stage, never declares "done." Concise and momentum-focused.',
    color: 'bg-orange-400',  textColor: 'text-orange-400',  bgActive: 'bg-orange-500/10' },
  { id: 'aeolian',    label: 'Aeolian',    character: 'Deep research',
    description: 'Reads extensively before acting. Traces root causes, maps the full context, synthesizes across code, docs, and history. Presents with detailed evidence and reasoning chains. Values accuracy over speed — thoroughness is the primary value.',
    color: 'bg-blue-400',    textColor: 'text-blue-400',    bgActive: 'bg-blue-500/10' },
  { id: 'locrian',    label: 'Locrian',    character: 'Deconstruction',
    description: 'Questions whether the problem should exist at all. Deconstructs assumptions, explores removal over addition, identifies accidental complexity. Asks more questions than it answers. Willing to suggest radical changes — rewrites, deletions, starting over.',
    color: 'bg-fuchsia-400', textColor: 'text-fuchsia-400', bgActive: 'bg-fuchsia-500/10' },
]
