import { create } from 'zustand'
import { registerSignOutTeardown } from './authStore'
import { fetchJson, _gmailSendError } from './googleAuthStore'

// ── Types (mirror captain_claw/flight_deck/gmail_autoreply_routes.py) ──

// Whether a topic may be switched on: `reason` when not; a `note` or a
// `warning` to show when it may (gmail_autoreply_config.dry_run_gate).
export interface AutoReplyGate {
  ok: boolean
  reason: string
  note: string
  warning: string
}

// A topic's last breadth test (server-managed).
export interface AutoReplyDryRun {
  match_hash: string
  at: string        // ISO 8601, UTC; '' = never run
  eligible: number  // recent inbox messages that passed the prefilters
  matched: number   // of those, classified as this topic
  rate: number      // matched / eligible
  since?: string    // when this wording was first tested (a re-test keeps it)
}

export interface AutoReplySources {
  folder: string    // 'vfs:<project>/<path>', '' = none
  insights: boolean
  playbooks: boolean
  web_search: boolean
}

// What the owner writes for one topic — the PUT's `topics` items. A topic
// without an `id` (or with one the server doesn't know) is a new topic.
export interface AutoReplyTopicInput {
  id?: string
  name: string
  enabled: boolean
  about: string             // "Emails that…"
  not_this: string
  examples: string[]
  counter_examples: string[]
  senders: string[]         // addresses / @domains; [] = any verified sender
  brief: string             // what the agent may say — PUBLIC
  sources: AutoReplySources
}

export interface AutoReplyTopic extends AutoReplyTopicInput {
  id: string
  dry_run: AutoReplyDryRun
  gate: AutoReplyGate
  // Folder passages the writer never sees because they look sensitive.
  hidden_passages: { file: string; hidden: number }[]
  folder_error: string  // '' | 'unresolvable' | 'not_a_directory' (deleted since chosen)
}

// The classifier or judge a stage would use now: 'jev', 'tier' or '' (none).
export interface AutoReplyChecker {
  source: string
  model: string
  provider: string
  tier: string
  fallback: AutoReplyChecker | null
}

export interface AutoReplyAgentState {
  ref: string
  name: string
  running: boolean
  shared: boolean   // has members: the insights and playbooks lookups are off
  eligible: boolean
  reason: string    // '' when it may write the replies
}

export interface AutoReplyStatus {
  deck_disabled: boolean       // FD_GMAIL_AUTOREPLY=off
  send_deck_disabled: boolean  // FD_GMAIL_SEND=off
  google_connected: boolean
  account_email: string        // the CONNECTED Google account
  bound_ok: boolean            // the connected account is the one turned on with
  scopes_ok: boolean
  missing_scopes: string[]
  signature_set: boolean
  agent: AutoReplyAgentState
  classifier: AutoReplyChecker
  judge: AutoReplyChecker
  sent_last_24h: number
  held_open: number
  last_poll_at: string
  last_error: string
  disabled_reason: string
  state: 'off' | 'on' | 'attention'
  // The requirement codes turning on would be refused for right now.
  missing: string[]
}

// GET/PUT /fd/google/gmail-autoreply. Off by default.
export interface AutoReplyView {
  enabled: boolean
  agent_ref: string
  daily_limit: number
  never_disclose: string[]
  topics: AutoReplyTopic[]
  account_email: string   // the Google account it was turned on for
  enabled_at: string
  disabled_reason: string
  updated_at: string
  status: AutoReplyStatus
  notice?: string         // a PUT's: 're-run the breadth test' when an edit switched a topic off
}

export interface AutoReplyPatch {
  enabled?: boolean
  agent_ref?: string
  daily_limit?: number
  never_disclose?: string[]
  topics?: AutoReplyTopicInput[]
}

// One of the user's agents for the picker (GET …/gmail-autoreply/agents).
export interface AutoReplyAgent {
  ref: string
  name: string
  runtime: string
  running: boolean
  shared: boolean
  eligible: boolean
  reason: string
}

// A masked local-scan finding: never the value, at most its first and last
// two characters (none for secrets).
export interface AutoReplyFinding {
  kind: string
  level: string   // 'block' | 'hold'
  hint: string
  echo: boolean   // the value was already in the inbound email
}

export interface AutoReplyJudge {
  source: string
  model: string
  passed: boolean
  failing: string[]
  scores: Record<string, number | boolean>
  note: string
  unavailable: boolean
  detail: string
}

// One ledger row (GET …/gmail-autoreply/activity). `reply_text` is only set
// for sent, maybe-sent ('unknown') and held rows.
export interface AutoReplyActivity {
  id: string
  state: string
  reason: string
  detail: string
  from: string
  subject: string
  topic_id: string
  topic_name: string
  confidence: number | null
  match_judge: string
  sources_used: string[]
  findings: AutoReplyFinding[]
  judge: Partial<AutoReplyJudge>
  agent_name: string
  thread_id: string
  inbound_message_id: string
  sent_message_id: string
  reply_text: string
  received_at: string
  created_at: string
  updated_at: string
}

export interface AutoReplyDryRunRow {
  from: string
  subject: string
  verdict: string   // 'match' | a match reason | 'not eligible: <code>'
  fit: string
  confidence: number | null
}

export interface AutoReplyDryRunResult {
  topic_id: string
  eligible: number
  matched: number
  rate: number
  rows: AutoReplyDryRunRow[]
  gate: AutoReplyGate | null
}

export interface AutoReplyTestInput {
  topic_id?: string
  from?: string     // blank = TRY_IT_SAMPLE_FROM (tryItSender): the writer needs someone to answer
  subject: string
  body: string
}

export interface AutoReplyMatch {
  topic_id: string
  reason: string
  candidate: string
  source: string
  model: string
  confidence: number | null
  fit: string
  detail: string
}

// "Try it": every stage of one made-up email. Nothing is sent or recorded.
export interface AutoReplyTestResult {
  match: AutoReplyMatch | null
  topic: { id: string; name: string } | null
  forced: boolean   // the topic was picked, not matched
  writer: {
    state: string
    reason: string
    detail?: string
    plan?: Record<string, string[]>
    sources_used?: string[]
    reply?: string
  } | null
  guard: { level: string; findings: AutoReplyFinding[] } | null
  judge: AutoReplyJudge | null
  outcome: 'skipped' | 'waiting' | 'failed' | 'blocked' | 'held' | 'would_send'
}

export interface AutoReplyHeldSent {
  ok: true
  message_id: string
  thread_id: string
  to: string
  subject: string
  row: AutoReplyActivity | null
}

export type AutoReplyResult<T> = { ok: true; data: T } | { ok: false; error: string }

// ── Pure helpers ────────────────────────────────────────────────────
//
// Kept free of imports: tests/test_flight_deck/test_gmail_autoreply_ui.py
// lifts them out of this file and runs them in Node.

// Topic limits (gmail_autoreply_config).
export const AUTO_REPLY_LIMITS = {
  maxTopics: 10,
  maxEnabledTopics: 5,
  dailyLimit: [1, 50],
  neverDisclose: 50,
  neverDiscloseChars: [2, 80],
  name: [3, 60],
  about: [20, 400],
  aboutMinWords: 6,
  notThisMax: 400,
  example: [10, 300],
  maxExamples: 5,
  maxCounterExamples: 5,
  maxSenders: 50,
  brief: [50, 4000],
  folderMax: 512,
} as const

// What each requirement code (gmail_autoreply_routes.REQUIREMENTS) asks of
// the user, for the list under a locked switch.
export const AUTO_REPLY_REQUIREMENTS: Record<string, string> = {
  deck_disabled: 'The deck administrator has turned topic auto-replies off on this Flight Deck (FD_GMAIL_AUTOREPLY=off).',
  send_deck_disabled: 'The deck administrator has turned email sending off on this Flight Deck (FD_GMAIL_SEND=off).',
  google_not_connected: 'Connect Google.',
  missing_scopes: 'Google must allow reading and sending mail — the deck admin adds the scope, then you reconnect Google.',
  no_signature: 'Set your agentic signature under Email sending — every auto-reply ends with it.',
  no_agent: 'Choose the agent that writes the replies.',
  agent_ineligible: "The chosen agent can't write replies — choose another.",
  agent_unavailable: "Flight Deck can't read its agent records right now — try again.",
  no_topics: 'Turn on at least one topic.',
  topic_gate: 'Every topic that is on needs a passing breadth test.',
  no_classifier: 'Nothing can check incoming mail — connect Jev, or set a Balanced or Reason model in the Library.',
  no_judge: 'Nothing can check replies before they go out — connect Jev, or set a Reason or Balanced model in the Library.',
  bound_elsewhere: 'Another user on this Flight Deck already has topic auto-replies on for this Google account.',
  baseline: "Flight Deck couldn't read your inbox to start from — try again.",
}

// The words for one requirement code, naming the missing Google scopes when
// they're known. An unknown code reads as itself.
export function requirementText(code: string, missingScopes: readonly string[] = []): string {
  if (code === 'missing_scopes' && missingScopes.length > 0) {
    return `Google must allow ${missingScopes.join(' and ')} — the deck admin adds the scope, then you reconnect Google.`
  }
  return AUTO_REPLY_REQUIREMENTS[code] || code
}

// The switch refuses only turning auto-replies ON while something they need is
// missing, and says what. Turning them off is always allowed. Keyed on the
// SAVED view (status.missing is the server's own list), like
// gmailSendOptInLocked. [] = not locked.
export function autoReplyOptInLocked(view: AutoReplyView | null): string[] {
  if (!view || view.enabled) return []
  const status = view.status
  const codes = Array.isArray(status?.missing) ? status.missing : []
  const scopes = Array.isArray(status?.missing_scopes) ? status.missing_scopes : []
  const out: string[] = []
  for (const code of codes) {
    const text = requirementText(code, scopes)
    if (!out.includes(text)) out.push(text)
  }
  return out
}

// gmail_autoreply_config._one_line: control and format characters dropped
// (tabs and line breaks kept, then folded), whitespace runs one space.
export function oneLine(text: string): string {
  return String(text ?? '')
    .replace(/[\p{Cc}\p{Cf}]/gu, (ch) => (ch === '\n' || ch === '\t' ? ch : ''))
    .split(/\s+/)
    .filter(Boolean)
    .join(' ')
}

// gmail_autoreply_config._multi_line (the brief): line breaks kept, trailing
// spaces and surrounding blank lines trimmed, at most one blank line in a row.
export function multiLine(text: string): string {
  const t = String(text ?? '')
    .replace(/\r\n?/g, '\n')
    .replace(/[\p{Cc}\p{Cf}]/gu, (ch) => (ch === '\n' || ch === '\t' ? ch : ''))
    .split('\n')
    .map((line) => line.replace(/\s+$/, ''))
    .join('\n')
    .trim()
  return t.replace(/\n{3,}/g, '\n\n')
}

// Characters as the server counts them (code points, not UTF-16 units).
function charCount(text: string): number {
  return [...text].length
}

const FOLD_EXTRA: Record<string, string> = { 'đ': 'd', 'ł': 'l', 'ø': 'o', 'ı': 'i', 'æ': 'ae', 'œ': 'oe' }

// gmail_autoreply_config.fold: case- and diacritic-folded ("Općenito Đak" →
// "opcenito dak").
export function foldText(text: string): string {
  return String(text ?? '')
    .toLowerCase()
    .replace(/ß/g, 'ss')
    .replace(/[đłøıæœ]/g, (ch) => FOLD_EXTRA[ch] ?? ch)
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
}

// The server lint's catch-all words and phrases (gmail_autoreply_config
// _BROAD_FIRST_WORDS / _BROAD_PHRASE_RE), matched on folded text.
const BROAD_FIRST_WORDS = new Set([
  'any', 'all', 'every', 'anything', 'everything', 'general', 'misc', 'other',
  'whatever', 'sve', 'svi', 'bilo', 'opcenito',
])
const BROAD_PHRASE_RE = /(?<![\p{L}\p{N}_-])((?:all e?mails|any e?mail|any question|anything else|everything|general inquir|sv(?:i|e|ih|im|ima) e?mailov|sva pitanja|bilo sto)[\p{L}\p{N}_]*)/u
const EMAIL_WORD_RE = /(?<![\p{L}\p{N}_-])e[\s-]?mail/gu
const FIRST_WORD_RE = /[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/u
const WORD_RE = /[\p{L}\p{N}]+/gu

function lintText(label: string, text: string): string {
  const folded = foldText(text).replace(EMAIL_WORD_RE, 'email').split(/\s+/).filter(Boolean).join(' ')
  const first = FIRST_WORD_RE.exec(folded)
  if (first && BROAD_FIRST_WORDS.has(first[0])) {
    return `Too broad: the ${label} starts with "${first[0]}". Name one concrete subject and the kind of request.`
  }
  const phrase = BROAD_PHRASE_RE.exec(folded)
  if (phrase) {
    return `Too broad: the ${label} says "${phrase[1]}". Name one concrete subject and the kind of request.`
  }
  return ''
}

function lintWords(about: string): string {
  const words = String(about ?? '').match(WORD_RE) || []
  return words.length < AUTO_REPLY_LIMITS.aboutMinWords
    ? `Too broad: describe the emails in at least ${AUTO_REPLY_LIMITS.aboutMinWords} words — the subject and the kind of request.`
    : ''
}

const NO_EXAMPLE = 'Too broad: add at least one example of an email that should match.'

// The server's "Too broad: …" lint (gmail_autoreply_config.lint_topic), word
// for word: the first reason the topic would be refused for, '' if none.
export function lintTopic(name: string, about: string, examples: readonly string[]): string {
  return lintText('name', name)
    || lintText('description', about)
    || lintWords(about)
    || ((examples || []).some((e) => String(e).trim()) ? '' : NO_EXAMPLE)
}

function lengthError(what: string, lo: number, hi: number, got: number): string {
  return lo <= 0
    ? `${what} can be at most ${hi} characters (got ${got}).`
    : `${what} must be ${lo} to ${hi} characters (got ${got}).`
}

// gmail_compose.normalize_allowlist, for a topic's senders: lowercased,
// a bare domain written '@domain', repeats dropped. `bad` lists what
// isn't an address or a domain.
const SENDER_ADDRESS_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
const SENDER_DOMAIN_RE = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/

export function normalizeSenders(entries: readonly string[]): { senders: string[]; bad: string[] } {
  const senders: string[] = []
  const bad: string[] = []
  for (const raw of entries || []) {
    const e = String(raw).trim().toLowerCase()
    if (!e) continue
    let norm = ''
    if (e.startsWith('@')) norm = SENDER_DOMAIN_RE.test(e.slice(1)) ? e : ''
    else if (e.includes('@')) norm = SENDER_ADDRESS_RE.test(e) ? e : ''
    else norm = SENDER_DOMAIN_RE.test(e) ? `@${e}` : ''
    if (!norm) bad.push(String(raw).trim())
    else if (!senders.includes(norm)) senders.push(norm)
  }
  return { senders, bad }
}

// gmail_autoreply_config._folder: 'vfs:<project>/<path>' ('' = none), or
// null when it can't name a folder in the user's files.
export function normalizeFolder(value: string): string | null {
  const raw = String(value ?? '').trim().replace(/\\/g, '/')
  if (!raw) return ''
  if (/[\p{Cc}\p{Cf}]/u.test(raw)) return null
  const rest = raw.slice(0, 4).toLowerCase() === 'vfs:' ? raw.slice(4) : raw
  const parts = rest.split('/').filter((p) => p !== '' && p !== '.')
  if (parts.length === 0 || parts.includes('..')) return null
  return `vfs:${parts[0]}/${parts.slice(1).join('/')}`
}

export type AutoReplyTopicField =
  | 'name' | 'about' | 'not_this' | 'examples' | 'counter_examples' | 'senders' | 'brief' | 'folder'

export type AutoReplyTopicErrors = Partial<Record<AutoReplyTopicField, string>>

function exampleErrors(list: readonly string[], noun: string, max: number): string {
  const [lo, hi] = AUTO_REPLY_LIMITS.example
  const kept = (list || []).map(oneLine).filter(Boolean)
  for (const text of kept) {
    const n = charCount(text)
    if (n < lo || n > hi) return lengthError(`Each ${noun}`, lo, hi, n)
  }
  if (kept.length > max) return `At most ${max} ${noun}s (got ${kept.length}).`
  return ''
}

// The inline errors of one topic, field by field: the server's range checks
// and its "Too broad" lint (lintTopic), so Save lights up only for a topic
// the server would take. {} = none. The server stays the judge (it also
// refuses a never-disclose term in a topic, and a folder that isn't one).
export function validateTopic(t: AutoReplyTopicInput): AutoReplyTopicErrors {
  const L = AUTO_REPLY_LIMITS
  const errors: AutoReplyTopicErrors = {}
  const name = oneLine(t.name)
  const about = oneLine(t.about)
  const nameLen = charCount(name)
  if (nameLen < L.name[0] || nameLen > L.name[1]) {
    errors.name = lengthError('The name', L.name[0], L.name[1], nameLen)
  } else {
    const broad = lintText('name', name)
    if (broad) errors.name = broad
  }
  const aboutLen = charCount(about)
  if (aboutLen < L.about[0] || aboutLen > L.about[1]) {
    errors.about = lengthError('The description', L.about[0], L.about[1], aboutLen)
  } else {
    const broad = lintText('description', about) || lintWords(about)
    if (broad) errors.about = broad
  }
  const notThisLen = charCount(oneLine(t.not_this))
  if (notThisLen > L.notThisMax) {
    errors.not_this = lengthError('"Not this"', 0, L.notThisMax, notThisLen)
  }
  const examples = exampleErrors(t.examples, 'example', L.maxExamples)
  if (examples) errors.examples = examples
  else if (!(t.examples || []).some((e) => oneLine(e))) errors.examples = NO_EXAMPLE
  const counter = exampleErrors(t.counter_examples, 'near-miss example', L.maxCounterExamples)
  if (counter) errors.counter_examples = counter
  const { senders, bad } = normalizeSenders(t.senders)
  if (bad.length > 0) {
    errors.senders = `Not an address or a domain: ${bad.slice(0, 10).join(', ')}${bad.length > 10 ? ' …' : ''}. Use alice@example.com or @example.com.`
  } else if (senders.length > L.maxSenders) {
    errors.senders = `At most ${L.maxSenders} senders (got ${senders.length}).`
  }
  const briefLen = charCount(multiLine(t.brief))
  if (briefLen < L.brief[0] || briefLen > L.brief[1]) {
    errors.brief = lengthError('What the agent may say', L.brief[0], L.brief[1], briefLen)
  }
  const folder = normalizeFolder(t.sources?.folder ?? '')
  if (folder === null) {
    errors.folder = 'Choose a folder in your files (vfs:<project>/<folder>).'
  } else if (charCount(folder) > L.folderMax) {
    errors.folder = lengthError('The folder', 0, L.folderMax, charCount(folder))
  }
  return errors
}

// "Try it" with From left blank is an email from this made-up guest: the
// writer answers a sender, so a sample without one could only ever fail.
export const TRY_IT_SAMPLE_FROM = 'guest@example.com'

// The From a "Try it" email is sent as: what was typed, else the made-up guest.
export function tryItSender(from: string | undefined): string {
  return String(from ?? '').trim() || TRY_IT_SAMPLE_FROM
}

// gmail_compose.recipients_allowed for one address against a topic's
// normalized senders: [] = anyone; else the exact address or its '@domain'.
export function senderAllowed(senders: readonly string[], address: string): boolean {
  if (!senders || senders.length === 0) return true
  const a = String(address ?? '').trim().toLowerCase()
  const at = a.lastIndexOf('@')
  const domain = at >= 0 ? a.slice(at + 1) : ''
  return senders.includes(a) || (!!domain && senders.includes(`@${domain}`))
}

// Gmail's own view of a thread, in the account the replies come from
// (best-effort: Gmail picks another account if that one isn't signed in).
export function gmailThreadUrl(accountEmail: string, threadId: string): string {
  if (!threadId || !/^[0-9a-zA-Z]+$/.test(threadId)) return ''
  const who = accountEmail ? `?authuser=${encodeURIComponent(accountEmail)}` : ''
  return `https://mail.google.com/mail/${who}#all/${threadId}`
}

const AUTO_REPLY_STALE = "This Flight Deck doesn't offer topic auto-replies yet — it needs an update and a restart."

// The backend's words for a failed call. These routes answer 404 only with a
// sentence of their own ("No such topic", "No such auto-reply."): FastAPI's
// bare 404, the SPA catch-all's "No route for /fd/…" and a 405 mean a deck
// that predates them. Everything else reads as Email sending's errors do.
export function autoReplyError(exc: unknown): string {
  const e = (exc && typeof exc === 'object' ? exc : {}) as { status?: unknown; detail?: unknown }
  if (typeof e.status === 'number') {
    const detail = typeof e.detail === 'string' ? e.detail : ''
    if (e.status === 405) return AUTO_REPLY_STALE
    if (e.status === 404) {
      return !detail || detail === 'Not Found' || detail.startsWith('No route for ')
        ? AUTO_REPLY_STALE
        : detail
    }
  }
  return _gmailSendError(exc)
}

// ── Store ───────────────────────────────────────────────────────────

const BASE = '/fd/google/gmail-autoreply'

interface GmailAutoReplyStore {
  config: AutoReplyView | null
  activity: AutoReplyActivity[]
  agents: AutoReplyAgent[]
  error: string | null

  fetchConfig: () => Promise<void>
  // Any subset of the fields; `notice` is the server's (a topic switched off).
  saveConfig: (patch: AutoReplyPatch) => Promise<{ ok: boolean; notice: string }>
  pause: () => Promise<boolean>
  fetchAgents: () => Promise<void>
  fetchActivity: (all?: boolean) => Promise<void>
  dryRun: (topicId: string) => Promise<AutoReplyResult<AutoReplyDryRunResult>>
  tryIt: (input: AutoReplyTestInput) => Promise<AutoReplyResult<AutoReplyTestResult>>
  sendHeld: (rowId: string, text?: string) => Promise<AutoReplyResult<AutoReplyHeldSent>>
  dismissHeld: (rowId: string) => Promise<AutoReplyResult<AutoReplyActivity | null>>
}

async function _attempt<T>(call: () => Promise<T>): Promise<AutoReplyResult<T>> {
  try {
    return { ok: true, data: await call() }
  } catch (exc) {
    return { ok: false, error: autoReplyError(exc) }
  }
}

// The user's own config, activity and held cards — per user like the Google
// account itself, so no admin is needed (and none can see others').
export const useGmailAutoReplyStore = create<GmailAutoReplyStore>((set, get) => ({
  config: null,
  activity: [],
  agents: [],
  error: null,

  fetchConfig: async () => {
    set({ error: null })
    try {
      set({ config: (await fetchJson(BASE)) as AutoReplyView })
    } catch (exc) {
      set({ error: autoReplyError(exc) })
    }
  },

  saveConfig: async (patch) => {
    // Every edit names the settings it was made on (updated_at), so a tab
    // left open since can't silently undo a newer change (the server answers
    // 409). Turning off must always go through: when a save that turns off
    // is refused as a whole (stale, or an edit the server won't take), the
    // switch-off is sent again on its own — and only it.
    const seen = get().config?.updated_at
    const { enabled, ...rest } = patch
    const edits = Object.values(rest).some((v) => v !== undefined)
    const body = seen && (edits || enabled !== false) ? { ...patch, updated_at: seen } : patch
    set({ error: null })
    try {
      const view = (await fetchJson(BASE, { method: 'PUT', body: JSON.stringify(body) })) as AutoReplyView
      set({ config: view })
      return { ok: true, notice: view.notice || '' }
    } catch (exc) {
      const message = autoReplyError(exc)
      if (enabled === false && edits) {
        try {
          const view = (await fetchJson(BASE, {
            method: 'PUT',
            body: JSON.stringify({ enabled: false }),
          })) as AutoReplyView
          set({
            config: view,
            error: `Topic auto-replies are off now, but your other changes weren't saved: ${message}`,
          })
        } catch (offExc) {
          set({ error: autoReplyError(offExc) })
        }
        return { ok: false, notice: '' }
      }
      set({ error: message })
      // A 409 wrote nothing, but what's missing may have changed since the
      // view was read: re-read it quietly, keeping the error on show.
      if ((exc as { status?: number })?.status === 409) {
        try { set({ config: (await fetchJson(BASE)) as AutoReplyView }) } catch { /* keep the old view */ }
      }
      return { ok: false, notice: '' }
    }
  },

  pause: async () => {
    set({ error: null })
    try {
      set({ config: (await fetchJson(`${BASE}/pause`, { method: 'POST', body: '{}' })) as AutoReplyView })
      return true
    } catch (exc) {
      set({ error: autoReplyError(exc) })
      return false
    }
  },

  fetchAgents: async () => {
    try {
      const data = await fetchJson(`${BASE}/agents`)
      set({ agents: Array.isArray(data) ? (data as AutoReplyAgent[]) : [] })
    } catch (exc) {
      set({ error: autoReplyError(exc) })
    }
  },

  fetchActivity: async (all = false) => {
    try {
      const data = await fetchJson(`${BASE}/activity?limit=50&all=${all ? 'true' : 'false'}`)
      set({ activity: Array.isArray(data) ? (data as AutoReplyActivity[]) : [] })
    } catch (exc) {
      set({ error: autoReplyError(exc) })
    }
  },

  dryRun: async (topicId) => {
    const out = await _attempt(async () => (await fetchJson(
      `${BASE}/topics/${encodeURIComponent(topicId)}/dry-run`,
      { method: 'POST', body: '{}' },
    )) as AutoReplyDryRunResult)
    // The test is stored on the topic: show its new gate and rate.
    if (out.ok) await get().fetchConfig()
    return out
  },

  // Always as someone: From left blank is the made-up guest (tryItSender).
  tryIt: (input) => _attempt(async () => (await fetchJson(`${BASE}/test`, {
    method: 'POST',
    body: JSON.stringify({ ...input, from: tryItSender(input.from) }),
  })) as AutoReplyTestResult),

  // No text = the held reply as it is; text = Edit & send.
  sendHeld: (rowId, text) => _attempt(async () => (await fetchJson(
    `${BASE}/activity/${encodeURIComponent(rowId)}/send`,
    { method: 'POST', body: JSON.stringify(text === undefined ? {} : { text }) },
  )) as AutoReplyHeldSent),

  dismissHeld: (rowId) => _attempt(async () => {
    const data = await fetchJson(`${BASE}/activity/${encodeURIComponent(rowId)}/dismiss`, {
      method: 'POST',
      body: '{}',
    })
    return (data?.row ?? null) as AutoReplyActivity | null
  }),
}))

// The folder picker's lists: the user's own files (/fd/vfs), so a topic's
// folder is one Flight Deck can resolve for them. [] on any error.
export async function listFolderProjects(): Promise<string[]> {
  try {
    const data = await fetchJson('/fd/vfs/projects')
    const projects = Array.isArray(data?.projects) ? data.projects : []
    return projects
      .filter((p: { name?: unknown; shared?: boolean; missing?: boolean }) =>
        typeof p?.name === 'string' && !p.shared && !p.missing)
      .map((p: { name: string }) => p.name)
  } catch {
    return []
  }
}

export async function listSubfolders(project: string, path: string): Promise<string[]> {
  try {
    const q = `project=${encodeURIComponent(project)}&path=${encodeURIComponent(path)}`
    const data = await fetchJson(`/fd/vfs/list?${q}`)
    const entries = Array.isArray(data?.entries) ? data.entries : []
    return entries
      .filter((e: { type?: string; name?: unknown }) =>
        e?.type === 'dir' && typeof e.name === 'string' && !e.name.startsWith('.'))
      .map((e: { name: string }) => e.name)
  } catch {
    return []
  }
}

// The next person signing in at this screen must not see the outgoing user's
// topics, never-disclose terms, held replies or agents.
registerSignOutTeardown(() => {
  useGmailAutoReplyStore.setState({ config: null, activity: [], agents: [], error: null })
})
