import { useEffect, useId, useMemo, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import {
  Scale,
  Loader2,
  Check,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Eye,
  EyeOff,
  Save,
  Trash2,
  Server,
  ExternalLink,
  FlaskConical,
  Play,
  Plus,
  X,
  RotateCcw,
  Braces,
  Copy,
} from 'lucide-react'
import {
  useJevStore,
  jevErrorText,
  HttpError,
  type JevAskBody,
  type JevAskResponse,
  type JevQuestion,
  type JevQuestionType,
} from '../../stores/jevStore'

// Jev (TypeSafe AI): calibrated yes/no, choice and score answers about text.
// One key per Flight Deck user (per deck when sign-in is off), kept by Flight
// Deck — agents, flows and MCP clients ask through it and never see the key.
// The "Try it" playground is this card's own state: nothing here is saved,
// and mounting the card only reads Flight Deck's record (never calls Jev).

// jev_client's limits, checked here first so Run explains instead of failing.
const MAX_STATE_CHARS = 120_000
const MIN_OPTIONS = 2
const MAX_OPTIONS = 255
const MIN_LEVELS = 2
const MAX_LEVELS = 10

// ── playground drafts ───────────────────────────────────────────────

interface OptionDraft {
  id: number
  name: string
  description: string
}

interface LevelDraft {
  id: number
  text: string
}

// Every type's fields live on the draft, so switching a question's type and
// back keeps what was typed.
interface QuestionDraft {
  id: number
  name: string
  type: JevQuestionType
  instructions: string
  yesMeans: string
  noMeans: string
  options: OptionDraft[]
  levels: LevelDraft[]
}

let _seq = 0
const nextId = () => ++_seq

const option = (name = '', description = ''): OptionDraft => ({ id: nextId(), name, description })
const level = (text = ''): LevelDraft => ({ id: nextId(), text })

function blankQuestion(name: string, type: JevQuestionType = 'noul'): QuestionDraft {
  return {
    id: nextId(),
    name,
    type,
    instructions: '',
    yesMeans: '',
    noMeans: '',
    options: [option(), option()],
    levels: [level(), level(), level()],
  }
}

const EXAMPLE_STATE =
  'Hi — I was charged twice for my March invoice (INV-2291) and I need the duplicate ' +
  'refunded before Friday, when my card statement closes. This is the second time this ' +
  'has happened.\n\n— Dana, Acme Ltd'

function exampleQuestions(): QuestionDraft[] {
  return [
    {
      ...blankQuestion('is_urgent', 'noul'),
      instructions: 'Does the customer need this resolved by a specific, near deadline?',
      yesMeans: 'A concrete, near deadline or explicit time pressure',
      noMeans: 'No time pressure expressed',
    },
    {
      ...blankQuestion('department', 'choice'),
      instructions: 'Which team should handle this message?',
      options: [
        option('billing', 'Payments, invoices, refunds and charges'),
        option('technical', 'Bugs, outages, login or integration problems'),
        option('sales', 'Pricing, upgrades and new accounts'),
      ],
    },
    {
      ...blankQuestion('frustration', 'score'),
      instructions: 'How frustrated is the customer?',
      levels: [
        level('Calm and neutral'),
        level('Mildly annoyed'),
        level('Clearly frustrated'),
        level('Angry, threatening to leave'),
      ],
    },
  ]
}

function freeName(drafts: QuestionDraft[]): string {
  const used = new Set(drafts.map((q) => q.name.trim()))
  for (let n = drafts.length + 1; ; n++) if (!used.has(`q${n}`)) return `q${n}`
}

// Characters as Python counts them (code points: an emoji is one, not two).
function codePoints(s: string): number {
  let pairs = 0
  for (let i = 0; i < s.length - 1; i++) {
    const c = s.charCodeAt(i)
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = s.charCodeAt(i + 1)
      if (d >= 0xdc00 && d <= 0xdfff) { pairs++; i++ }
    }
  }
  return s.length - pairs
}

// The length of Python's json.dumps(value, ensure_ascii=False) — ", " and
// ": " separators, no indentation — which is how Flight Deck sizes JSON state.
function pyJsonLength(v: unknown): number {
  if (Array.isArray(v)) return 2 + v.reduce<number>((n, x, i) => n + pyJsonLength(x) + (i ? 2 : 0), 0)
  if (v && typeof v === 'object') {
    return 2 + Object.entries(v).reduce<number>(
      (n, [k, x], i) => n + codePoints(JSON.stringify(k)) + 2 + pyJsonLength(x) + (i ? 2 : 0), 0,
    )
  }
  return codePoints(JSON.stringify(v) ?? 'null')
}

// jev_client.parse_state's size: JSON object / array text re-serialised (so
// pretty-printing costs nothing), anything else as text.
function stateSize(raw: string): number {
  const t = raw.trim()
  if (t[0] === '{' || t[0] === '[') {
    try {
      const parsed: unknown = JSON.parse(t)
      if (parsed && typeof parsed === 'object') return pyJsonLength(parsed)
    } catch { /* plain text */ }
  }
  return codePoints(t)
}

type Built = { body: JevAskBody; problem?: undefined } | { body?: undefined; problem: string }

// The /fd/jev/ask body, or the first thing that keeps it from being one.
// Blank option / level rows are ignored, so a spare row never blocks Run.
function buildRequest(state: string, drafts: QuestionDraft[], model: string): Built {
  const text = state.trim()
  if (!text) return { problem: 'Add the state — the text the questions are about.' }
  const size = stateSize(text)
  if (size > MAX_STATE_CHARS) {
    return {
      problem: `The state is ${size.toLocaleString()} characters; Jev reads about ` +
        `${MAX_STATE_CHARS.toLocaleString()}. Keep only what the questions need.`,
    }
  }
  if (drafts.length === 0) return { problem: 'Add at least one question.' }
  // Null-prototype maps: a question or option named "__proto__" stays a key.
  const questions = Object.create(null) as Record<string, JevQuestion>
  for (const [i, q] of drafts.entries()) {
    const name = q.name.trim()
    if (!name) return { problem: `Question ${i + 1} needs a name.` }
    if (Object.hasOwn(questions, name)) return { problem: `Two questions are named "${name}".` }
    const instructions = q.instructions.trim()
    if (!instructions) return { problem: `"${name}" needs its question.` }
    if (q.type === 'noul') {
      const criteria: Record<string, string> = {}
      if (q.yesMeans.trim()) criteria.true = q.yesMeans.trim()
      if (q.noMeans.trim()) criteria.false = q.noMeans.trim()
      questions[name] = { type: 'noul', instructions, ...(Object.keys(criteria).length ? { criteria } : {}) }
    } else if (q.type === 'choice') {
      const criteria = Object.create(null) as Record<string, string | null>
      for (const o of q.options) {
        const optName = o.name.trim()
        const desc = o.description.trim()
        if (!optName && !desc) continue
        if (!optName) return { problem: `Every option of "${name}" needs a name.` }
        if (Object.hasOwn(criteria, optName)) return { problem: `"${name}" lists the option "${optName}" twice.` }
        criteria[optName] = desc || null
      }
      const n = Object.keys(criteria).length
      if (n < MIN_OPTIONS) return { problem: `"${name}" needs at least ${MIN_OPTIONS} options.` }
      if (n > MAX_OPTIONS) return { problem: `"${name}" has ${n} options; Jev takes at most ${MAX_OPTIONS}.` }
      questions[name] = { type: 'choice', instructions, criteria }
    } else {
      const levels = q.levels.map((l) => l.text.trim()).filter(Boolean)
      if (levels.length < MIN_LEVELS) return { problem: `"${name}" needs at least ${MIN_LEVELS} levels.` }
      if (levels.length > MAX_LEVELS) return { problem: `"${name}" has ${levels.length} levels; Jev takes at most ${MAX_LEVELS}.` }
      questions[name] = { type: 'score', instructions, criteria: levels }
    }
  }
  return { body: { state: text, questions, ...(model ? { model } : {}) } }
}

function isJsonState(text: string): boolean {
  const t = text.trim()
  if (t[0] !== '{' && t[0] !== '[') return false
  try {
    const parsed: unknown = JSON.parse(t)
    return !!parsed && typeof parsed === 'object'
  } catch {
    return false
  }
}

// ── formatting ──────────────────────────────────────────────────────

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const clamp01 = (n: number) => Math.min(1, Math.max(0, n))
const pct = (p: number) => `${Math.round(clamp01(p) * 100)}%`

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function numbers(v: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  for (const [k, x] of Object.entries(asRecord(v))) if (isNum(x)) out[k] = x
  return out
}

function text(v: unknown): string {
  if (v === null || v === undefined) return ''
  return typeof v === 'string' ? v : JSON.stringify(v)
}

function duration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`
}

function when(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

// ── shared classes ──────────────────────────────────────────────────

const pill = 'inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border'
const pillTiny = 'inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border'
const field =
  'w-full min-w-0 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-xs text-zinc-200 placeholder-zinc-600 focus:border-violet-500/50 focus:outline-none disabled:opacity-50'
const label = 'block text-[11px] font-medium uppercase tracking-wider text-zinc-500 mb-1'
const iconBtn =
  'shrink-0 rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-zinc-500'
const addBtn =
  'inline-flex items-center gap-1.5 rounded-md border border-zinc-700 hover:bg-zinc-800 px-2.5 py-1 text-xs text-zinc-300 disabled:opacity-40'
const linkBtn = 'inline-flex items-center gap-1 text-[11px] text-zinc-500 hover:text-zinc-200 disabled:opacity-40'

const TYPE_LABEL: Record<JevQuestionType, string> = { noul: 'Yes / no', choice: 'Choice', score: 'Score' }
const TYPE_HINT: Record<JevQuestionType, string> = {
  noul: 'A yes/no question — Jev answers with the probability that it is yes.',
  choice: 'Pick one option out of 2–255 you name — Jev answers with each option’s probability.',
  score: 'Rate on an ordered scale of 2–10 levels, lowest first — Jev answers with the expected level.',
}
const INSTRUCTIONS_PLACEHOLDER: Record<JevQuestionType, string> = {
  noul: 'The yes/no question, e.g. "Does the customer ask for a refund?"',
  choice: 'What to decide, e.g. "Which team should handle this?"',
  score: 'What to rate, e.g. "How frustrated is the customer?"',
}

// ── the card ────────────────────────────────────────────────────────

type Notice = { tone: 'ok' | 'warn'; text: string }
type Busy = null | 'save' | 'test' | 'remove'

export default function JevConnection() {
  const { status, loading, error, refresh, saveKey, removeKey, test } = useJevStore()

  const [collapsed, setCollapsed] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')
  const [reveal, setReveal] = useState(false)
  const [busy, setBusy] = useState<Busy>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const keyId = useId()

  // Flight Deck's record only (GET /fd/jev/status) — no call to Jev.
  useEffect(() => { refresh() }, [refresh])

  const keySet = !!status?.key_set
  const deckScope = status?.scope === 'deck'

  const onSave = async (e: FormEvent) => {
    e.preventDefault()
    const key = keyDraft.trim()
    if (!key || busy) return
    setBusy('save'); setNotice(null)
    const res = await saveKey(key)
    setBusy(null)
    if (!res.ok) return
    setKeyDraft('')
    setReveal(false)
    if (res.warning) {
      setNotice({ tone: 'warn', text: res.warning })
    } else {
      const models = useJevStore.getState().status?.models || []
      setNotice({
        tone: 'ok',
        text: `Saved and verified with Jev.${models.length ? ` Models: ${models.join(', ')}.` : ''}`,
      })
    }
  }

  const onTest = async () => {
    if (busy) return
    setBusy('test'); setNotice(null)
    const res = await test()
    setBusy(null)
    if (!res) return
    const names = res.models.map((m) => m.name).filter(Boolean)
    setNotice({
      tone: 'ok',
      text: `Jev accepted the key and answered in ${duration(res.latency_ms)}.` +
        (names.length ? ` Models: ${names.join(', ')}.` : ''),
    })
  }

  const onRemove = async () => {
    if (busy) return
    const ok = window.confirm(
      deckScope
        ? "Remove this deck's Jev API key?\n\nEvery agent, flow and MCP client on this deck loses Jev until a key is added again."
        : 'Remove your Jev API key?\n\nYour agents, flows and MCP clients lose Jev until you add a key again.',
    )
    if (!ok) return
    setBusy('remove'); setNotice(null)
    const done = await removeKey()
    setBusy(null)
    if (done) setNotice({ tone: 'ok', text: 'Key removed.' })
  }

  let statusPill: ReactNode
  if (!status && error && !loading) {
    statusPill = (
      <span className={`${pill} bg-red-500/15 text-red-600 dark:text-red-500 border-red-500/30`}>
        <AlertCircle className="h-3 w-3" /> Unavailable
      </span>
    )
  } else if (!status) {
    statusPill = (
      <span className={`${pill} bg-zinc-800 text-zinc-400 border-zinc-700`}>
        <Loader2 className="h-3 w-3 animate-spin" /> Checking
      </span>
    )
  } else if (keySet && status.verified === false && status.verify_error === 'refused') {
    statusPill = (
      <span
        className={`${pill} bg-red-500/15 text-red-600 dark:text-red-500 border-red-500/30`}
        title="Jev refused this key — it may have been revoked. Paste a new key."
      >
        <AlertCircle className="h-3 w-3" /> Key refused
      </span>
    )
  } else if (keySet && status.verified === false) {
    statusPill = (
      <span
        className={`${pill} bg-amber-500/15 text-amber-600 dark:text-amber-500 border-amber-500/30`}
        title="The key is saved, but Jev couldn't be reached to check it. Test checks it again."
      >
        <AlertCircle className="h-3 w-3" /> Saved, not verified
      </span>
    )
  } else if (keySet) {
    statusPill = (
      <span className={`${pill} bg-emerald-500/15 text-emerald-600 dark:text-emerald-500 border-emerald-500/30`}>
        <Check className="h-3 w-3" /> Connected
      </span>
    )
  } else {
    statusPill = <span className={`${pill} bg-zinc-800 text-zinc-400 border-zinc-700`}>Not connected</span>
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 overflow-hidden">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className={
          'w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-zinc-900/80 transition-colors ' +
          (collapsed ? '' : 'border-b border-zinc-800')
        }
        aria-expanded={!collapsed}
      >
        <div className="h-10 w-10 shrink-0 rounded-md bg-gradient-to-br from-amber-500/20 to-violet-500/20 border border-zinc-800 flex items-center justify-center">
          <Scale className="h-5 w-5 text-zinc-200" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-zinc-100">Jev</h3>
            {statusPill}
          </div>
          <div className="text-xs text-zinc-500 mt-0.5">
            TypeSafe AI's decision model — calibrated yes/no, choice and score answers about text
          </div>
        </div>
        {loading && status && <Loader2 className="h-4 w-4 shrink-0 animate-spin text-zinc-500" />}
        <ChevronDown className={'h-4 w-4 shrink-0 text-zinc-500 transition-transform ' + (collapsed ? '-rotate-90' : '')} />
      </button>

      {/* Hidden, not unmounted: a drafted playground survives collapsing. */}
      <div hidden={collapsed} className="px-5 py-4 space-y-4">
        <p className="text-xs leading-relaxed text-zinc-400">
          Jev doesn't write text. You give it some content — the <span className="text-zinc-200">state</span> —
          and named questions, and it answers each one in a fraction of a second with calibrated
          probabilities: a yes/no, one option out of a set, or a level on a scale. Your agents get a{' '}
          <code className="text-zinc-300">jev</code> tool for triage, routing, classification and checking
          claims against a source; flows can run it as a step and MCP clients through{' '}
          <code className="text-zinc-300">jev_ask</code>. Flight Deck keeps the key — agents ask through
          Flight Deck and never see it.
        </p>

        {error && (
          <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            <span className="min-w-0 flex-1 break-words">{error}</span>
            {!status && !loading && (
              <button type="button" onClick={() => refresh()} className="shrink-0 underline hover:no-underline">
                Try again
              </button>
            )}
          </div>
        )}

        {notice && (
          <div
            className={
              'rounded-md border px-3 py-2 text-xs break-words ' +
              (notice.tone === 'ok'
                ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                : 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300')
            }
          >
            {notice.text}
          </div>
        )}

        {status && (
          <form onSubmit={onSave} className="space-y-2">
            <label htmlFor={keyId} className="block text-xs font-medium text-zinc-400">
              API key
              {keySet && <span className="ml-2 text-[10px] text-emerald-600 dark:text-emerald-500">(saved)</span>}
            </label>
            <div className="relative">
              <input
                id={keyId}
                type={reveal ? 'text' : 'password'}
                value={keyDraft}
                onChange={(e) => setKeyDraft(e.target.value)}
                placeholder={
                  keySet
                    ? `•••• saved (${status.key_hint || '····'}) — paste a new key to replace`
                    : 'Paste your Jev API key'
                }
                autoComplete="off"
                spellCheck={false}
                disabled={busy === 'save'}
                className="w-full min-w-0 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 pr-9 text-sm text-zinc-200 font-mono placeholder-zinc-600 focus:border-violet-500 focus:outline-none disabled:opacity-50"
              />
              <button
                type="button"
                onClick={() => setReveal((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
                tabIndex={-1}
                aria-label={reveal ? 'Hide key' : 'Show key'}
              >
                {reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </button>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="submit"
                disabled={!keyDraft.trim() || busy !== null}
                className="inline-flex items-center gap-2 rounded-md bg-violet-600 hover:bg-violet-500 px-3 py-1.5 text-sm text-white disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
                title="Flight Deck checks the key with Jev before saving it"
              >
                {busy === 'save' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                {busy === 'save' ? 'Verifying…' : 'Save'}
              </button>
              {keySet && (
                <button
                  type="button"
                  onClick={onTest}
                  disabled={busy !== null}
                  className="inline-flex items-center gap-1.5 rounded-md bg-zinc-800 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
                  title="Ask Jev which models this key can use"
                >
                  {busy === 'test' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Server className="h-3 w-3" />}
                  Test
                </button>
              )}
              {keySet && (
                <button
                  type="button"
                  onClick={onRemove}
                  disabled={busy !== null}
                  className="inline-flex items-center gap-1.5 rounded-md border border-red-500/30 hover:bg-red-500/10 px-3 py-1.5 text-xs text-red-600 dark:text-red-500 disabled:opacity-50"
                >
                  {busy === 'remove' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
                  Remove key
                </button>
              )}
            </div>

            {keySet && (
              <div className="text-[11px] leading-snug text-zinc-500">
                Key <code className="text-zinc-300">{status.key_hint || '····'}</code>
                {status.verified === true
                  ? <> · verified{status.verified_at ? ` ${when(status.verified_at)}` : ''}</>
                  : status.verified === false && status.verify_error === 'refused'
                    ? <> · <span className="text-red-600 dark:text-red-400">Jev refused it</span> — it may have been revoked; paste a new key above.</>
                    : status.verified === false
                      ? <> · not verified — Jev couldn't be reached to check it. Test checks it again.</>
                      : <> · not checked yet</>}
                {status.models.length > 0 && (
                  <> · models: <span className="text-zinc-400">{status.models.join(', ')}</span></>
                )}
              </div>
            )}

            <div className="text-[11px] leading-snug text-zinc-500">
              {deckScope
                ? "Sign-in is off on this deck, so the key belongs to the deck: every agent, flow and MCP client on it uses it."
                : 'Your own key: your agents, flows and MCP clients use it. Every other Flight Deck user adds their own.'}{' '}
              Get one at{' '}
              <a
                href="https://console.typesafe.ai/keys"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 text-violet-500 hover:underline"
              >
                console.typesafe.ai/keys <ExternalLink className="h-3 w-3" />
              </a>{' '}
              · how to ask good questions:{' '}
              <a
                href="https://docs.typesafe.ai"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 text-violet-500 hover:underline"
              >
                docs.typesafe.ai <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          </form>
        )}

        {keySet && status && (
          <Playground defaultModel={status.default_model || 'jev-latest'} models={status.models} />
        )}
      </div>
    </div>
  )
}

// ── Try it ──────────────────────────────────────────────────────────

interface RunResult {
  request: JevAskBody
  response: JevAskResponse
}

function Playground({ defaultModel, models }: { defaultModel: string; models: string[] }) {
  const ask = useJevStore((s) => s.ask)
  const refresh = useJevStore((s) => s.refresh)
  const [state, setState] = useState(EXAMPLE_STATE)
  const [questions, setQuestions] = useState<QuestionDraft[]>(exampleQuestions)
  const [modelChoice, setModelChoice] = useState('')
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [result, setResult] = useState<RunResult | null>(null)
  const stateId = useId()

  const model = modelChoice || defaultModel
  const modelOptions = useMemo(
    () => Array.from(new Set([defaultModel, ...models, model].filter(Boolean))),
    [defaultModel, models, model],
  )
  const built = useMemo(() => buildRequest(state, questions, model), [state, questions, model])
  const jsonState = useMemo(() => isJsonState(state), [state])
  const size = useMemo(() => stateSize(state), [state])
  const edited = useMemo(
    () => !!result && !!built.body && JSON.stringify(built.body) !== JSON.stringify(result.request),
    [built, result],
  )

  const run = async () => {
    if (running || !built.body) return
    const body = built.body
    setRunning(true); setRunError(null)
    try {
      setResult({ request: body, response: await ask(body) })
    } catch (exc) {
      setRunError(jevErrorText(exc))
      // The key went away meanwhile (another tab, an admin): show it.
      if (exc instanceof HttpError && exc.status === 404) void refresh()
    } finally {
      setRunning(false)
    }
  }

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      void run()
    }
  }

  const update = (id: number, patch: Partial<QuestionDraft>) =>
    setQuestions((qs) => qs.map((q) => (q.id === id ? { ...q, ...patch } : q)))
  const remove = (id: number) => setQuestions((qs) => qs.filter((q) => q.id !== id))
  const add = () => setQuestions((qs) => [...qs, blankQuestion(freeName(qs))])

  const loadExample = () => {
    setState(EXAMPLE_STATE)
    setQuestions(exampleQuestions())
    setResult(null); setRunError(null)
  }
  const clearAll = () => {
    setState('')
    setQuestions([blankQuestion('q1')])
    setResult(null); setRunError(null)
  }

  return (
    <section className="space-y-3 rounded-md border border-zinc-800 bg-zinc-950/50 p-2.5 sm:p-3" aria-label="Try Jev">
      <div className="flex items-start gap-2 flex-wrap">
        <div className="flex-1 min-w-[12rem]">
          <div className="flex items-center gap-1.5 text-sm font-medium text-zinc-200">
            <FlaskConical className="h-4 w-4 text-violet-500" /> Try it
          </div>
          <div className="text-[11px] leading-snug text-zinc-500 mt-0.5">
            Ask Jev about some text with your key. Nothing here is saved; each run counts toward your Jev usage.
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button type="button" onClick={loadExample} disabled={running} className={linkBtn}>
            <RotateCcw className="h-3 w-3" /> Example
          </button>
          <button type="button" onClick={clearAll} disabled={running} className={linkBtn}>
            <X className="h-3 w-3" /> Clear
          </button>
        </div>
      </div>

      <fieldset disabled={running} onKeyDown={onKeyDown} className="min-w-0 space-y-3">
        <div>
          <div className="flex items-baseline justify-between gap-2">
            <label htmlFor={stateId} className={label}>State</label>
            <span className="mb-1 flex items-center gap-1.5 text-[10px] text-zinc-500 tabular-nums">
              {jsonState && (
                <span
                  className={`${pillTiny} bg-blue-500/15 text-blue-600 dark:text-blue-400 border-blue-500/30`}
                  title="Sent as structured data — questions can point at its fields by name"
                >
                  <Braces className="h-2.5 w-2.5" /> JSON
                </span>
              )}
              <span className={size > MAX_STATE_CHARS ? 'text-red-600 dark:text-red-500' : ''}>
                {size.toLocaleString()} chars
              </span>
            </span>
          </div>
          <textarea
            id={stateId}
            value={state}
            onChange={(e) => setState(e.target.value)}
            rows={5}
            spellCheck={false}
            placeholder="The text the questions are about — a message, a document excerpt, a record. A JSON object or array is sent as structured data."
            className={`${field} font-mono resize-y leading-relaxed`}
          />
        </div>

        <div className="space-y-2">
          <div className={label}>Questions</div>
          {questions.map((q, i) => (
            <QuestionEditor
              key={q.id}
              q={q}
              index={i}
              canRemove={questions.length > 1}
              onChange={(patch) => update(q.id, patch)}
              onRemove={() => remove(q.id)}
            />
          ))}
          <button type="button" onClick={add} className={addBtn}>
            <Plus className="h-3.5 w-3.5" /> Add question
          </button>
        </div>

        <div className="flex items-center gap-x-3 gap-y-2 flex-wrap pt-1">
          <button
            type="button"
            onClick={() => void run()}
            disabled={running || !built.body}
            className="inline-flex items-center gap-2 rounded-md bg-violet-600 hover:bg-violet-500 px-3.5 py-1.5 text-sm text-white shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
            title={built.problem || 'Ask Jev (⌘/Ctrl + Enter)'}
          >
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            {running ? 'Asking Jev…' : 'Run'}
          </button>
          <label className="flex min-w-0 items-center gap-1.5 text-[11px] text-zinc-500">
            Model
            <select
              value={model}
              onChange={(e) => setModelChoice(e.target.value)}
              className="min-w-0 max-w-[11rem] rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 font-mono focus:border-violet-500/50 focus:outline-none disabled:opacity-50"
            >
              {modelOptions.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          <span className="hidden sm:inline text-[11px] text-zinc-600">⌘/Ctrl + Enter runs</span>
        </div>
      </fieldset>

      {built.problem && !running && (
        <div className="flex items-start gap-1.5 text-[11px] text-amber-700 dark:text-amber-400">
          <AlertCircle className="h-3 w-3 shrink-0 mt-0.5" />
          <span className="min-w-0 break-words">{built.problem}</span>
        </div>
      )}

      {runError && (
        <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span className="min-w-0 break-words">{runError}</span>
        </div>
      )}

      {result && (
        <Results result={result} running={running} edited={edited} />
      )}
    </section>
  )
}

function TypeSwitch({ value, onChange }: { value: JevQuestionType; onChange: (t: JevQuestionType) => void }) {
  return (
    <div role="radiogroup" aria-label="Question type" className="inline-flex shrink-0 rounded-md border border-zinc-700 bg-zinc-950 p-0.5">
      {(['noul', 'choice', 'score'] as const).map((t) => {
        const selected = value === t
        return (
          <button
            key={t}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(t)}
            title={TYPE_HINT[t]}
            className={
              'rounded px-2 py-0.5 text-[11px] whitespace-nowrap transition-colors disabled:opacity-50 ' +
              (selected ? 'bg-violet-600 text-white' : 'text-zinc-400 hover:text-zinc-200')
            }
          >
            {TYPE_LABEL[t]}
          </button>
        )
      })}
    </div>
  )
}

function QuestionEditor({
  q, index, canRemove, onChange, onRemove,
}: {
  q: QuestionDraft
  index: number
  canRemove: boolean
  onChange: (patch: Partial<QuestionDraft>) => void
  onRemove: () => void
}) {
  const setOption = (id: number, patch: Partial<OptionDraft>) =>
    onChange({ options: q.options.map((o) => (o.id === id ? { ...o, ...patch } : o)) })
  const setLevel = (id: number, textValue: string) =>
    onChange({ levels: q.levels.map((l) => (l.id === id ? { ...l, text: textValue } : l)) })
  const n = index + 1

  return (
    <div className="space-y-2 rounded-md border border-zinc-800 bg-zinc-900/40 p-2.5">
      <div className="flex items-center gap-2 flex-wrap">
        <input
          value={q.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="name"
          aria-label={`Question ${n} name`}
          spellCheck={false}
          className={`${field} font-mono flex-1 basis-32 sm:flex-none sm:w-40`}
          title="The answer comes back under this name (flows read it as steps.<id>.values.<name>)"
        />
        <TypeSwitch value={q.type} onChange={(type) => onChange({ type })} />
        <button
          type="button"
          onClick={onRemove}
          disabled={!canRemove}
          className={`${iconBtn} ml-auto`}
          title="Remove question"
          aria-label={`Remove question ${n}`}
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <textarea
        value={q.instructions}
        onChange={(e) => onChange({ instructions: e.target.value })}
        rows={2}
        placeholder={INSTRUCTIONS_PLACEHOLDER[q.type]}
        aria-label={`Question ${n}`}
        className={`${field} resize-y`}
      />

      {q.type === 'noul' && (
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="min-w-0">
            <div className={label}>Yes means <span className="normal-case tracking-normal text-zinc-600">(optional)</span></div>
            <input
              value={q.yesMeans}
              onChange={(e) => onChange({ yesMeans: e.target.value })}
              placeholder="what counts as yes"
              aria-label={`Question ${n}: what yes means`}
              className={field}
            />
          </div>
          <div className="min-w-0">
            <div className={label}>No means <span className="normal-case tracking-normal text-zinc-600">(optional)</span></div>
            <input
              value={q.noMeans}
              onChange={(e) => onChange({ noMeans: e.target.value })}
              placeholder="what counts as no"
              aria-label={`Question ${n}: what no means`}
              className={field}
            />
          </div>
        </div>
      )}

      {q.type === 'choice' && (
        <div className="space-y-1.5">
          <div className={label}>
            Options <span className="normal-case tracking-normal text-zinc-600">— a name, and what it covers</span>
          </div>
          {q.options.map((o, i) => (
            <div key={o.id} className="flex items-center gap-1.5">
              <div className="grid min-w-0 flex-1 grid-cols-[minmax(0,2fr)_minmax(0,3fr)] sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)] gap-1.5">
                <input
                  value={o.name}
                  onChange={(e) => setOption(o.id, { name: e.target.value })}
                  placeholder={`option ${i + 1}`}
                  aria-label={`Question ${n}, option ${i + 1} name`}
                  spellCheck={false}
                  className={`${field} font-mono`}
                />
                <input
                  value={o.description}
                  onChange={(e) => setOption(o.id, { description: e.target.value })}
                  placeholder="description (optional)"
                  aria-label={`Question ${n}, option ${i + 1} description`}
                  className={field}
                />
              </div>
              <button
                type="button"
                onClick={() => onChange({ options: q.options.filter((x) => x.id !== o.id) })}
                disabled={q.options.length <= MIN_OPTIONS}
                className={iconBtn}
                title="Remove option"
                aria-label={`Remove option ${i + 1}`}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={() => onChange({ options: [...q.options, option()] })}
            disabled={q.options.length >= MAX_OPTIONS}
            className={addBtn}
          >
            <Plus className="h-3 w-3" /> Add option
          </button>
        </div>
      )}

      {q.type === 'score' && (
        <div className="space-y-1.5">
          <div className={label}>
            Levels <span className="normal-case tracking-normal text-zinc-600">— lowest first, 2–10</span>
          </div>
          {q.levels.map((l, i) => (
            <div key={l.id} className="flex items-center gap-1.5">
              <span className="w-4 shrink-0 text-center text-[11px] tabular-nums text-zinc-500" aria-hidden>{i}</span>
              <input
                value={l.text}
                onChange={(e) => setLevel(l.id, e.target.value)}
                placeholder={i === 0 ? 'lowest level' : i === q.levels.length - 1 ? 'highest level' : `level ${i}`}
                aria-label={`Question ${n}, level ${i}`}
                className={field}
              />
              <button
                type="button"
                onClick={() => onChange({ levels: q.levels.filter((x) => x.id !== l.id) })}
                disabled={q.levels.length <= MIN_LEVELS}
                className={iconBtn}
                title="Remove level"
                aria-label={`Remove level ${i}`}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={() => onChange({ levels: [...q.levels, level()] })}
            disabled={q.levels.length >= MAX_LEVELS}
            className={addBtn}
          >
            <Plus className="h-3 w-3" /> Add level
          </button>
        </div>
      )}
    </div>
  )
}

// ── answers ─────────────────────────────────────────────────────────

function Results({ result, running, edited }: { result: RunResult; running: boolean; edited: boolean }) {
  const { request, response } = result
  const answers = asRecord(response.answers)
  // The questions' order, then anything else Jev sent back.
  const names = [
    ...Object.keys(request.questions).filter((k) => k in answers),
    ...Object.keys(answers).filter((k) => !(k in request.questions)),
  ]
  const usage = asRecord(response.usage)
  const meta = [
    response.model ? `model ${response.model}` : '',
    isNum(usage.input_tokens) ? `${usage.input_tokens.toLocaleString()} in` : '',
    isNum(usage.output_tokens) ? `${usage.output_tokens.toLocaleString()} out tokens` : '',
    isNum(response.latency_ms) ? duration(response.latency_ms) : '',
  ].filter(Boolean)

  return (
    <div className={'space-y-2 transition-opacity ' + (running ? 'opacity-50' : '')} aria-live="polite">
      <div className="flex items-baseline justify-between gap-x-3 gap-y-0.5 flex-wrap border-t border-zinc-800 pt-3">
        <div className="text-xs font-medium text-zinc-300">
          Answers
          {edited && <span className="ml-2 font-normal text-[11px] text-zinc-500">· inputs changed since — Run again</span>}
        </div>
        {meta.length > 0 && <div className="text-[11px] text-zinc-500 tabular-nums">{meta.join(' · ')}</div>}
      </div>

      {names.length === 0 && (
        <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2 text-xs text-zinc-400">
          Jev returned no answers.
        </div>
      )}

      <div className="grid gap-2">
        {names.map((name) => (
          <AnswerCard key={name} name={name} question={request.questions[name]} answer={answers[name]} />
        ))}
      </div>

      <details className="group rounded-md border border-zinc-800 bg-zinc-950/50">
        <summary className="flex cursor-pointer select-none list-none items-center gap-1.5 px-3 py-2 text-xs text-zinc-400 hover:text-zinc-200 [&::-webkit-details-marker]:hidden">
          <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
          <Braces className="h-3.5 w-3.5" /> Raw JSON
        </summary>
        <div className="space-y-3 border-t border-zinc-800 px-3 py-2.5">
          <CodeBlock title="Request" body={JSON.stringify(request, null, 2)} />
          <CodeBlock title="Response" body={JSON.stringify(response, null, 2)} />
          {response.summary && <CodeBlock title="What an agent reads" body={response.summary} />}
        </div>
      </details>
    </div>
  )
}

function CodeBlock({ title, body }: { title: string; body: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(body)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard blocked — the text is selectable */ }
  }
  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[10px] font-medium uppercase tracking-wider text-zinc-500">{title}</span>
        <button type="button" onClick={copy} className={linkBtn} title={`Copy ${title.toLowerCase()}`}>
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded border border-zinc-800 bg-zinc-900 p-2 font-mono text-[11px] leading-relaxed text-zinc-300">
        {body}
      </pre>
    </div>
  )
}

function Bar({ value, className }: { value: number; className: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-zinc-800">
      <div className={`h-full rounded-full transition-[width] duration-500 ${className}`} style={{ width: pct(value) }} />
    </div>
  )
}

function ProbRow({ label: rowLabel, p, highlight }: { label: string; p: number; highlight: boolean }) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className={'min-w-0 truncate ' + (highlight ? 'font-medium text-zinc-100' : 'text-zinc-400')} title={rowLabel}>
          {rowLabel}
        </span>
        <span className={'shrink-0 tabular-nums ' + (highlight ? 'text-zinc-200' : 'text-zinc-500')}>{pct(p)}</span>
      </div>
      <Bar value={p} className={highlight ? 'bg-violet-500' : 'bg-zinc-600'} />
    </div>
  )
}

function Confidence({ value }: { value: unknown }) {
  if (!isNum(value)) return null
  const low = value < 0.5
  return (
    <span
      className={
        `${pillTiny} tabular-nums ` +
        (low
          ? 'bg-amber-500/15 text-amber-600 dark:text-amber-500 border-amber-500/30'
          : 'bg-zinc-800 text-zinc-400 border-zinc-700')
      }
      title={
        low
          ? 'Low confidence: the probability is spread out, so Jev has no clear read. Sharpen the question or the options, or leave this one to a person.'
          : 'How concentrated the probabilities are: 1 = all on one answer, 0 = spread evenly.'
      }
    >
      confidence {value.toFixed(2)}
    </span>
  )
}

function AnswerCard({ name, question, answer }: { name: string; question: JevQuestion | undefined; answer: unknown }) {
  const a = asRecord(answer)
  const kind = typeof a.type === 'string' ? a.type : ''
  const typeLabel = kind === 'noul' || kind === 'choice' || kind === 'score' ? TYPE_LABEL[kind] : kind || 'answer'

  let body: ReactNode
  if (kind === 'noul' && isNum(a.noul)) body = <NoulAnswer p={clamp01(a.noul)} question={question} />
  else if (kind === 'choice') body = <ChoiceAnswer a={a} question={question} />
  else if (kind === 'score' && isNum(a.score)) body = <ScoreAnswer a={a} score={a.score} />
  else {
    body = (
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-zinc-400">
        {JSON.stringify(answer, null, 2)}
      </pre>
    )
  }

  return (
    <div className="min-w-0 space-y-2 rounded-md border border-zinc-800 bg-zinc-900/40 p-2.5 sm:p-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="min-w-0 break-all font-mono text-xs text-zinc-200">{name}</span>
        <span className={`${pillTiny} bg-zinc-800 text-zinc-400 border-zinc-700`}>{typeLabel}</span>
        <span className="ml-auto"><Confidence value={a.confidence} /></span>
      </div>
      {question && typeof question.instructions === 'string' && (
        <div className="text-[11px] leading-snug text-zinc-500 line-clamp-2" title={question.instructions}>
          {question.instructions}
        </div>
      )}
      {body}
    </div>
  )
}

function NoulAnswer({ p, question }: { p: number; question: JevQuestion | undefined }) {
  // jev_client's reading: ≥ 0.65 yes, ≤ 0.35 no, uncertain between.
  const verdict = p >= 0.65 ? 'yes' : p <= 0.35 ? 'no' : 'uncertain'
  const tone = {
    yes: { text: 'text-emerald-600 dark:text-emerald-400', bar: 'bg-emerald-500' },
    no: { text: 'text-red-600 dark:text-red-400', bar: 'bg-red-500' },
    uncertain: { text: 'text-amber-600 dark:text-amber-400', bar: 'bg-amber-500' },
  }[verdict]
  const criteria = question && !Array.isArray(question.criteria) ? question.criteria : undefined
  const meaning = verdict === 'yes' ? criteria?.true : verdict === 'no' ? criteria?.false : undefined
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className={`text-base font-semibold capitalize ${tone.text}`}>{verdict}</span>
        <span className="text-xs tabular-nums text-zinc-500">p(yes) = {p.toFixed(2)}</span>
        {meaning && <span className="min-w-0 text-[11px] text-zinc-500">— {meaning}</span>}
      </div>
      <div className="relative">
        <Bar value={p} className={tone.bar} />
        <div className="absolute left-1/2 -top-0.5 h-2.5 w-px bg-zinc-500" aria-hidden />
      </div>
      <div className="flex justify-between text-[10px] text-zinc-600">
        <span>no</span>
        <span>unsure</span>
        <span>yes</span>
      </div>
    </div>
  )
}

const CHOICE_SHOWN = 8

function ChoiceAnswer({ a, question }: { a: Record<string, unknown>; question: JevQuestion | undefined }) {
  const [all, setAll] = useState(false)
  const chosen = text(a.choice)
  const probs = Object.entries(numbers(a.probabilities)).sort((x, y) => y[1] - x[1])
  const shown = all ? probs : probs.slice(0, CHOICE_SHOWN)
  const criteria = question && !Array.isArray(question.criteria) ? question.criteria : undefined
  const description = criteria?.[chosen]
  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="min-w-0 break-all text-base font-semibold text-zinc-100">{chosen || '—'}</span>
        {description && <span className="min-w-0 text-[11px] text-zinc-500">— {description}</span>}
      </div>
      {shown.length > 0 && (
        <div className="space-y-1.5">
          {shown.map(([opt, p]) => <ProbRow key={opt} label={opt} p={p} highlight={opt === chosen} />)}
        </div>
      )}
      {probs.length > CHOICE_SHOWN && (
        <button type="button" onClick={() => setAll((v) => !v)} className={linkBtn}>
          {all ? 'Show fewer' : `Show all ${probs.length} options`}
        </button>
      )}
    </div>
  )
}

function ScoreAnswer({ a, score }: { a: Record<string, unknown>; score: number }) {
  const legend = asRecord(a.legend)
  const probs = numbers(a.probabilities)
  const levels = Array.from(new Set([...Object.keys(legend), ...Object.keys(probs)]))
    .map(Number)
    .filter(Number.isInteger)
    .sort((x, y) => x - y)
  const lo = levels.length ? levels[0] : 0
  const hi = levels.length ? levels[levels.length - 1] : 0
  const nearest = Math.min(hi, Math.max(lo, Math.round(score)))
  const nearestText = text(legend[String(nearest)])
  const pos = (x: number) => `${hi > lo ? (Math.min(hi, Math.max(lo, x)) - lo) / (hi - lo) * 100 : 50}%`

  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="text-base font-semibold tabular-nums text-zinc-100">{score.toFixed(2)}</span>
        {levels.length > 1 && <span className="text-xs text-zinc-500">on {lo}–{hi}</span>}
      </div>
      {nearestText && (
        <div className="min-w-0 break-words text-xs text-zinc-400">
          Nearest level {nearest}: <span className="text-zinc-200">“{nearestText}”</span>
        </div>
      )}
      {levels.length > 1 && (
        <div className="relative mx-1.5 h-4" aria-hidden>
          <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-zinc-800" />
          {levels.map((k) => (
            <div key={k} className="absolute top-1/2 h-2.5 w-px -translate-y-1/2 bg-zinc-600" style={{ left: pos(k) }} />
          ))}
          <div
            className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-violet-500 ring-2 ring-zinc-900 transition-[left] duration-500"
            style={{ left: pos(score) }}
          />
        </div>
      )}
      {levels.length > 0 && (
        <div className="space-y-1.5">
          {levels.map((k) => {
            const desc = text(legend[String(k)])
            return (
              <ProbRow
                key={k}
                label={desc ? `${k} · ${desc}` : String(k)}
                p={probs[String(k)] ?? 0}
                highlight={k === nearest}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}
