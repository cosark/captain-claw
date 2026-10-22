import { useState, useEffect, useCallback, useRef } from 'react'
import { X, Wand2, Loader2, AlertTriangle, Trash2, Pin, ListPlus, Paperclip, FileText } from 'lucide-react'
import { useAuthStore, refreshAccessToken } from '../../stores/authStore'
import { useChatStore, WORK_LANES, LANE_MAIN, laneKey } from '../../stores/chatStore'
import { uploadFileToAgent, formatSize } from '../../services/fileTransfer'
import { Notice } from '../console/parts'

// The `wide:` test (index.css): a phone's sheet scrolls as one column.
const WIDE_QUERY = '(min-width: 640px) and (min-height: 501px), (min-width: 640px) and (pointer: fine)'

/**
 * Turn one description of a repetitive job into a reviewed list of queue tasks.
 *
 * The plan is a PROPOSAL: nothing reaches the queue until "Send". The model
 * returns one template plus a list of ranges — never the messages themselves —
 * and the backend expands them, so every task is identical except its range
 * and no standing rule can be dropped between batch 3 and batch 19
 * (docs/queue-task-planner-plan.md).
 */

interface PlanResult {
  template: string
  batches: Record<string, unknown>[]
  messages: string[]
  rationale: string
  warnings: string[]
  facts: { table?: string; key_min?: number; key_max?: number; tables?: { name: string }[] }
}

async function fdPost<T>(path: string, body: unknown): Promise<T> {
  const { token, authEnabled } = useAuthStore.getState()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authEnabled && token) headers['Authorization'] = `Bearer ${token}`
  const call = () => fetch(`/fd${path}`, {
    method: 'POST', headers, credentials: 'include', body: JSON.stringify(body),
  })
  let res = await call()
  if (res.status === 401 && authEnabled && await refreshAccessToken()) res = await call()
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try { detail = (await res.json()).detail || detail } catch { /* keep status */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

// ── Remembering where a job got to ──
//
// A 1,818-row table is not one sitting. Reopening the modal tomorrow should
// know that last time covered 241–490, and offer the next stretch with the
// SAME template — continuing costs no model call at all, and can't drift into
// a different template for the second half of one job.
interface LastPlan {
  template: string
  lastTo: number
  keyColumn: string
  batchSize: number
  table: string
  count: number
  at: number
}

function lastPlanKey(agentId: string, table: string) {
  return `fd.queue.plan.${agentId}.${table || '_'}`
}

function loadLastPlan(agentId: string, table: string): LastPlan | null {
  try {
    const raw = window.localStorage.getItem(lastPlanKey(agentId, table))
    return raw ? (JSON.parse(raw) as LastPlan) : null
  } catch { return null }
}

function saveLastPlan(agentId: string, p: LastPlan) {
  try {
    window.localStorage.setItem(lastPlanKey(agentId, p.table), JSON.stringify(p))
  } catch { /* quota — the plan still went out */ }
}

function highestTo(batches: Record<string, unknown>[]): number {
  return batches.reduce((max, b) => {
    const v = Number(b.to ?? b.end ?? NaN)
    return Number.isFinite(v) && v > max ? v : max
  }, 0)
}

function ago(ms: number): string {
  const mins = Math.round((Date.now() - ms) / 60000)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  return hrs < 24 ? `${hrs}h ago` : `${Math.round(hrs / 24)}d ago`
}

interface TableInfo {
  name: string
  columns: { name: string; type: string }[]
  row_count: number
}

async function fdGet<T>(path: string): Promise<T> {
  const { token, authEnabled } = useAuthStore.getState()
  const headers: Record<string, string> = {}
  if (authEnabled && token) headers['Authorization'] = `Bearer ${token}`
  const call = () => fetch(`/fd${path}`, { headers, credentials: 'include' })
  let res = await call()
  if (res.status === 401 && authEnabled && await refreshAccessToken()) res = await call()
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json() as Promise<T>
}

export function QueuePlannerModal({ agentId, agentName, host, port, auth, onClose }: {
  agentId: string
  agentName: string
  host: string
  port: number
  auth: string
  onClose: () => void
}) {
  const enqueue = useChatStore((s) => s.enqueueQueueMessage)
  const sessions = useChatStore((s) => s.sessions)
  const activeLane = useChatStore((s) => s.activeLane[agentId] || LANE_MAIN)

  const [intent, setIntent] = useState('')
  const [table, setTable] = useState('')
  const [keyColumn, setKeyColumn] = useState('_id')
  const [batchSize, setBatchSize] = useState(10)
  const [maxTasks, setMaxTasks] = useState(50)
  // One lane became many: the reviewed tasks are dealt out across every lane
  // ticked here, not pushed onto one queue. At least one stays selected.
  const [lanes, setLanes] = useState<string[]>([activeLane])
  const [newSession, setNewSession] = useState(true)
  // Whether the queue moves on by itself after each task (the lanes' Auto).
  // Until the user touches it, it only shows what the ticked lanes do today
  // (checked when every one of them is on Auto) and Send changes no lane's
  // mode; once touched, Send sets it on each ticked lane.
  const [autoTouched, setAutoTouched] = useState(false)
  const [autoMove, setAutoMove] = useState(false)

  // Attachments live on the AGENT: the tasks reference the path at run time,
  // and the planner reads a short preview so it can see the ids/headers inside
  // instead of guessing at the very ranges we grounded in the datastore.
  const [files, setFiles] = useState<{ path: string; filename: string; size: number }[]>([])
  const [uploading, setUploading] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [plan, setPlan] = useState<PlanResult | null>(null)
  const [template, setTemplate] = useState('')
  const [messages, setMessages] = useState<string[]>([])
  // A message the user edited by hand is pinned: re-expanding the template
  // must not silently overwrite their correction.
  const [pinned, setPinned] = useState<Set<number>>(new Set())
  const [warnings, setWarnings] = useState<string[]>([])
  const [sent, setSent] = useState(0)
  // Where the last send went, for the confirmation (and which lanes are paused).
  const [sentTo, setSentTo] = useState<{ lanes: string[]; paused: string[]; switched: { lane: string; auto: boolean }[] }>({ lanes: [], paused: [], switched: [] })
  const [confirmAgain, setConfirmAgain] = useState(false)
  // A plan nobody added yet isn't thrown away by a stray Esc or backdrop click.
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const resultRef = useRef<HTMLDivElement>(null)
  // The agent's real tables and columns, so table and key are PICKED, not
  // typed. A typo in either is a plan built against nothing.
  const [tables, setTables] = useState<TableInfo[]>([])
  const [tablesError, setTablesError] = useState('')
  const [last, setLast] = useState<LastPlan | null>(() => loadLastPlan(agentId, ''))
  const [continueCount, setContinueCount] = useState(10)

  useEffect(() => {
    if (!host || !port) return
    const qs = auth ? `&token=${encodeURIComponent(auth)}` : ''
    fdGet<TableInfo[]>(`/agent-datastore/${host}/${port}/tables?_=1${qs}`)
      .then((data) => {
        const list = Array.isArray(data) ? data : []
        setTables(list)
        // Pre-select the table the planner would have defaulted to anyway, so
        // what's on screen matches what it will use.
        setTable((cur) => cur || (list[0]?.name ?? ''))
      })
      .catch((e) => setTablesError(e instanceof Error ? e.message : String(e)))
  }, [host, port, auth])

  useEffect(() => { setLast(loadLastPlan(agentId, table)) }, [agentId, table])

  // Switching tables can strand the key on a column the new table doesn't
  // have — which would plan against a filter that matches nothing.
  useEffect(() => {
    const cols = tables.find((t) => t.name === table)?.columns
    if (!cols) return
    if (keyColumn !== '_id' && !cols.some((c) => c.name === keyColumn)) setKeyColumn('_id')
  }, [table, tables, keyColumn])

  const unsent = !!plan && messages.length > 0 && sent === 0
  const requestClose = useCallback(() => {
    if (unsent) setConfirmDiscard(true)
    else onClose()
  }, [unsent, onClose])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Esc on the discard question means "keep editing".
      if (confirmDiscard) setConfirmDiscard(false)
      else requestClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [requestClose, confirmDiscard])

  // A phone shows the request above the plan: bring the plan into view.
  const showResult = () => {
    if (window.matchMedia(WIDE_QUERY).matches) return
    window.setTimeout(() => resultRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
  }

  const addFiles = async (list: FileList | null) => {
    if (!list || list.length === 0) return
    setUploading((n) => n + list.length)
    for (const file of Array.from(list)) {
      try {
        const up = await uploadFileToAgent(host, port, auth, file)
        setFiles((prev) => [...prev, { path: up.path, filename: up.filename, size: up.size }])
      } catch (e) {
        setError(`Upload failed for ${file.name}: ${e instanceof Error ? e.message : e}`)
      } finally {
        setUploading((n) => Math.max(0, n - 1))
      }
    }
  }

  const runPlan = async () => {
    if (!intent.trim() || busy) return
    setBusy(true); setError(''); setSent(0)
    try {
      const res = await fdPost<PlanResult>('/queue/plan', {
        intent, host, port, auth, table, key_column: keyColumn,
        batch_size: batchSize, max_tasks: maxTasks,
        files: files.map((f) => ({ path: f.path, filename: f.filename })),
      })
      setPlan(res)
      setTemplate(res.template)
      setMessages(res.messages)
      setWarnings(res.warnings || [])
      setPinned(new Set())
      setConfirmAgain(false)
      showResult()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const runContinue = async () => {
    if (!last || busy) return
    setBusy(true); setError(''); setSent(0)
    try {
      const res = await fdPost<PlanResult>('/queue/continue', {
        template: last.template,
        start: last.lastTo + 1,
        batch_size: last.batchSize,
        count: continueCount,
        max_tasks: maxTasks,
        host, port, auth, table: last.table, key_column: last.keyColumn,
      })
      setPlan(res)
      setTemplate(res.template)
      setMessages(res.messages)
      setWarnings(res.warnings || [])
      setPinned(new Set())
      setTable(last.table)
      setKeyColumn(last.keyColumn)
      setBatchSize(last.batchSize)
      setConfirmAgain(false)
      showResult()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // Editing the template re-renders every message — that's what "always write
  // in English" means when you notice it on task 4 of 25. Costs no LLM call:
  // the backend expands, so there's only ever one implementation of it.
  const reExpand = useCallback(async (tpl: string) => {
    if (!plan) return
    try {
      const res = await fdPost<{ messages: string[]; warnings: string[] }>(
        '/queue/expand', { template: tpl, batches: plan.batches, max_tasks: maxTasks })
      setMessages((prev) => res.messages.map((m, i) => (pinned.has(i) ? prev[i] : m)))
      setWarnings(res.warnings || [])
    } catch { /* keep the last good expansion */ }
  }, [plan, maxTasks, pinned])

  useEffect(() => {
    if (!plan || template === plan.template) return
    const t = setTimeout(() => void reExpand(template), 400)
    return () => clearTimeout(t)
  }, [template, plan, reExpand])

  const editMessage = (i: number, text: string) => {
    setMessages((prev) => prev.map((m, idx) => (idx === i ? text : m)))
    setPinned((prev) => new Set(prev).add(i))
  }

  const dropMessage = (i: number) => {
    setMessages((prev) => prev.filter((_, idx) => idx !== i))
    setPinned((prev) => {
      const next = new Set<number>()
      prev.forEach((p) => { if (p < i) next.add(p); else if (p > i) next.add(p - 1) })
      return next
    })
  }

  const send = () => {
    // Canonical A-B-C order regardless of the order lanes were ticked.
    const dest = WORK_LANES.filter((l) => lanes.includes(l))
    if (dest.length === 0 || messages.length === 0) return
    const chat = useChatStore.getState()
    // Opening a lane below also shows it: remember the lane on screen, so the
    // visible chat doesn't jump to whichever lane was opened last.
    const onScreen = chat.activeLane[agentId] || LANE_MAIN
    // A lane that has never been opened has no queue to land in — open each
    // target once so every one of them can receive its share.
    dest.forEach((l) => {
      if (!sessions.get(laneKey(agentId, l))) {
        useChatStore.getState().setActiveLane(agentId, l)
      }
    })
    // How each target lane moves on, before its first task can start — only
    // when the user chose it here. A lane that already had work and changes
    // mode is named in the confirmation.
    const switched: { lane: string; auto: boolean }[] = []
    if (autoTouched) {
      dest.forEach((l) => {
        const key = laneKey(agentId, l)
        const lane = useChatStore.getState().sessions.get(key)
        if (!lane || lane.queueAutoMode === autoMove) return
        if (lane.queue.some((q) => q.status !== 'done')) switched.push({ lane: l, auto: autoMove })
        useChatStore.getState().setQueueAutoMode(key, autoMove)
      })
    }
    // Deal the tasks out evenly, one lane after the next (round-robin), so the
    // per-lane counts never differ by more than one. Within a lane a fresh
    // session (/new) still precedes every task after its first, so each batch
    // stays as self-contained as it was on a single lane.
    const opened = new Set<string>()
    messages.forEach((m, i) => {
      const l = dest[i % dest.length]
      const key = laneKey(agentId, l)
      if (newSession && opened.has(l)) enqueue(key, '/new')
      opened.add(l)
      enqueue(key, m)
    })
    if ((useChatStore.getState().activeLane[agentId] || LANE_MAIN) !== onScreen) {
      useChatStore.getState().setActiveLane(agentId, onScreen)
    }
    setSent(messages.length)
    setSentTo({
      lanes: dest,
      paused: dest.filter((l) => useChatStore.getState().sessions.get(laneKey(agentId, l))?.queuePaused),
      switched,
    })
    setConfirmAgain(false)
    setConfirmDiscard(false)
    // Remember where this got to, so the next sitting can carry straight on.
    if (plan) {
      const to = highestTo(plan.batches)
      if (to > 0) {
        const record: LastPlan = {
          template, lastTo: to, keyColumn, batchSize,
          table: plan.facts?.table || table, count: messages.length, at: Date.now(),
        }
        saveLastPlan(agentId, record)
        setLast(record)
      }
    }
  }

  // `_id` is the implicit primary key — it isn't in the column list, and it's
  // the usual batching key, so it leads.
  const keyOptions = (() => {
    const cols = tables.find((t) => t.name === table)?.columns ?? []
    return ['_id', ...cols.map((c) => c.name).filter((n) => n !== '_id')]
  })()

  const laneLabel = (l: string) => `${l} - ${agentName}`
  const pendingIn = (l: string) =>
    sessions.get(laneKey(agentId, l))?.queue.filter((q) => q.status === 'pending').length || 0

  // Which lanes get the work, in canonical A-B-C order, and how the tasks split
  // across them. Counts are simulated with the SAME round-robin `send` uses, so
  // the breakdown on screen is exactly what will be queued.
  const targets = WORK_LANES.filter((l) => lanes.includes(l))
  // Untouched, the box mirrors the ticked lanes: checked only if all are on Auto.
  const autoChecked = autoTouched
    ? autoMove
    : targets.length > 0 && targets.every((l) => !!sessions.get(laneKey(agentId, l))?.queueAutoMode)
  const perLane = targets.map((l) => ({ lane: l, count: 0 }))
  if (perLane.length) messages.forEach((_, i) => { perLane[i % perLane.length].count++ })
  const newCount = perLane.reduce((s, p) => s + Math.max(0, p.count - 1), 0)
  const distText = targets.length <= 1
    ? `lane ${targets[0] ?? ''}`
    : perLane.map((p) => `${p.lane}·${p.count}`).join('  ')

  const toggleLane = (l: string) => {
    setLanes((prev) =>
      prev.includes(l)
        // At least one lane must stay selected — the last one won't turn off.
        ? (prev.length === 1 ? prev : prev.filter((x) => x !== l))
        : [...prev, l])
  }

  // What the send confirmation says: where the tasks went, and which lane
  // still has to be started by hand.
  const sentNotice = (() => {
    if (!sent) return ''
    const where = sentTo.lanes.length <= 1
      ? `Added ${sent} task${sent === 1 ? '' : 's'} to lane ${sentTo.lanes[0] ?? ''}.`
      : `Added ${sent} tasks across lanes ${sentTo.lanes.join(', ')}.`
    // (Its queue's button says Start, or Resume once something there ran.)
    const paused = sentTo.paused.length === 0 ? ''
      : sentTo.paused.length === 1 ? ` Lane ${sentTo.paused[0]} is paused: its tasks wait until you start it from its queue.`
      : ` Lanes ${sentTo.paused.join(', ')} are paused: their tasks wait until you start them from their queues.`
    const switched = sentTo.switched
      .map((x) => ` Lane ${x.lane} switched to ${x.auto ? 'Auto' : 'step by step'}.`)
      .join('')
    return where + switched + paused
  })()

  return (
    <div className="fixed inset-0 z-50 flex bg-black/60 wide:items-center wide:justify-center wide:p-4" onClick={requestClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Plan tasks"
        className="flex h-[100dvh] w-full flex-col overflow-hidden bg-zinc-900 shadow-2xl wide:h-[85vh] wide:max-w-6xl wide:rounded-2xl wide:border wide:border-zinc-700"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-2 border-b border-zinc-800 px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <Wand2 className="h-4 w-4 shrink-0 text-violet-400" />
            <h2 className="text-sm font-semibold text-zinc-100">Plan tasks</h2>
            <span className="hidden truncate text-xs text-zinc-500 wide:inline">— one description becomes a queue</span>
          </div>
          <button onClick={requestClose} aria-label="Close" className="rounded-lg p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300">
            <X className="h-4 w-4" />
          </button>
        </div>
        {confirmDiscard && (
          <div className="flex flex-wrap items-center gap-2 border-b border-amber-500/25 bg-amber-500/10 px-4 py-2.5 text-xs text-amber-800 dark:text-amber-200">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1">
              <strong className="font-semibold">Discard this plan?</strong>{' '}
              The {messages.length} task{messages.length === 1 ? '' : 's'} you planned haven't been added yet.
            </span>
            <div className="flex shrink-0 gap-1.5">
              <button
                onClick={onClose}
                className="rounded-lg border border-amber-500/40 px-2.5 py-1 text-[11px] font-medium hover:bg-amber-500/20"
              >
                Discard
              </button>
              <button
                autoFocus
                onClick={() => setConfirmDiscard(false)}
                className="rounded-lg bg-violet-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-violet-500"
              >
                Keep editing
              </button>
            </div>
          </div>
        )}

        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto wide:flex-row wide:overflow-hidden">
          {/* ── Request ── */}
          <div className="flex w-full flex-col gap-3 border-b border-zinc-800 p-4 wide:w-[380px] wide:shrink-0 wide:overflow-y-auto wide:border-b-0 wide:border-r">
            {last && (
              <div className="rounded-md border border-violet-500/30 bg-violet-500/10 p-2">
                <p className="text-[11px] text-violet-200">
                  Last plan covered {last.keyColumn} up to <strong>{last.lastTo}</strong>
                  {last.table ? ` in ${last.table}` : ''} — {last.count} tasks, {ago(last.at)}.
                </p>
                <div className="mt-1.5 flex items-center gap-1.5">
                  <span className="text-[10px] text-zinc-400">Continue with</span>
                  <input
                    type="number" min={1} max={200} value={continueCount}
                    onChange={(e) => setContinueCount(Number(e.target.value))}
                    className="w-14 rounded border border-zinc-700 bg-zinc-950 px-1 py-0.5 text-[11px] text-zinc-200"
                  />
                  <span className="text-[10px] text-zinc-400">
                    more, from {last.lastTo + 1}
                  </span>
                  <button
                    onClick={runContinue}
                    disabled={busy}
                    title="Same template, no model call"
                    className="ml-auto rounded-md bg-violet-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-violet-500 disabled:opacity-40"
                  >
                    Continue
                  </button>
                </div>
                <p className="mt-1 text-[9px] text-zinc-500">
                  Reuses the approved template — free, and can't drift from the first half.
                </p>
              </div>
            )}

            <label className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">
              What needs doing
            </label>
            <textarea
              value={intent}
              onChange={(e) => setIntent(e.target.value)}
              rows={10}
              placeholder={'Describe the whole job, including every standing rule the agent must follow each time.\n\ne.g. enrich fund_portfolio in batches of 10: research company_description, stage, investment_amount… always write in English. _id and id are identical, never do +1 on the id!'}
              className="w-full resize-y rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-xs leading-relaxed text-zinc-200 placeholder-zinc-600 focus:border-violet-500/60 focus:outline-none"
            />
            <p className="text-[10px] leading-relaxed text-zinc-500">
              Rules are copied into <em>every</em> task verbatim — each one runs in its own
              session and can't see the others.
            </p>

            {/* Attachments */}
            <div
              onDragOver={(e) => { e.preventDefault(); e.stopPropagation() }}
              onDrop={(e) => { e.preventDefault(); e.stopPropagation(); void addFiles(e.dataTransfer.files) }}
              className="rounded-md border border-dashed border-zinc-700 p-2"
            >
              <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-zinc-400 hover:text-zinc-200">
                <Paperclip className="h-3 w-3" />
                Attach files{uploading > 0 && <Loader2 className="h-3 w-3 animate-spin" />}
                <input type="file" multiple className="hidden"
                  onChange={(e) => { void addFiles(e.target.files); e.target.value = '' }} />
              </label>
              {files.length > 0 && (
                <ul className="mt-1.5 flex flex-col gap-1">
                  {files.map((f, i) => (
                    <li key={f.path} className="flex items-center gap-1.5 text-[10px] text-zinc-500">
                      <FileText className="h-3 w-3 shrink-0 text-zinc-600" />
                      <span className="truncate" title={f.path}>{f.filename}</span>
                      <span className="shrink-0 text-zinc-600">{formatSize(f.size)}</span>
                      <button
                        onClick={() => setFiles((prev) => prev.filter((_, idx) => idx !== i))}
                        className="ml-auto shrink-0 rounded p-0.5 text-zinc-600 hover:bg-red-500/20 hover:text-red-300"
                      >
                        <X className="h-2.5 w-2.5" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-1 text-[9px] leading-relaxed text-zinc-600">
                Uploaded to the agent. Tasks get the path; the planner reads the first
                rows so it can see what's inside.
              </p>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <Field label="Table">
                {tables.length > 0 ? (
                  <select value={table} onChange={(e) => setTable(e.target.value)} className={inputCls}>
                    {tables.map((t) => (
                      <option key={t.name} value={t.name}>
                        {t.name} ({t.row_count.toLocaleString()})
                      </option>
                    ))}
                  </select>
                ) : (
                  // The agent is unreachable — typing still beats being stuck.
                  <input value={table} onChange={(e) => setTable(e.target.value)}
                    placeholder={tablesError ? 'agent unreachable' : 'loading…'}
                    className={inputCls} />
                )}
              </Field>
              <Field label="Batch key">
                {keyOptions.length > 1 ? (
                  <select value={keyColumn} onChange={(e) => setKeyColumn(e.target.value)} className={inputCls}>
                    {keyOptions.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                ) : (
                  <input value={keyColumn} onChange={(e) => setKeyColumn(e.target.value)} className={inputCls} />
                )}
              </Field>
              <Field label="Rows per task">
                <input type="number" min={1} max={50} value={batchSize}
                  onChange={(e) => setBatchSize(Number(e.target.value))} className={inputCls} />
              </Field>
              <Field label="Max tasks">
                <input type="number" min={1} max={200} value={maxTasks}
                  onChange={(e) => setMaxTasks(Number(e.target.value))} className={inputCls} />
              </Field>
            </div>

            <Field label="Distribute to lanes">
              <div className="flex flex-wrap gap-1.5">
                {WORK_LANES.map((l) => {
                  const on = lanes.includes(l)
                  const pend = pendingIn(l)
                  return (
                    <button
                      key={l}
                      type="button"
                      onClick={() => toggleLane(l)}
                      aria-pressed={on}
                      className={`flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors ${
                        on
                          ? 'border-violet-500/60 bg-violet-600/20 text-violet-100'
                          : 'border-zinc-700 bg-zinc-950 text-zinc-400 hover:border-zinc-600 hover:text-zinc-300'
                      }`}
                    >
                      {laneLabel(l)}
                      {pend ? <span className="text-zinc-500">· {pend} pending</span> : null}
                    </button>
                  )
                })}
              </div>
              <p className="text-[10px] leading-relaxed text-zinc-500">
                {targets.length <= 1
                  ? 'All tasks go to this one lane.'
                  : `Tasks are dealt out evenly across ${targets.length} lanes (${distText}).`}
              </p>
            </Field>

            <label className="flex items-center gap-2 text-[11px] text-zinc-400" title="The agent starts each task with a clean context (/new between tasks)">
              <input type="checkbox" checked={newSession}
                onChange={(e) => setNewSession(e.target.checked)} className="accent-violet-500" />
              Start each task in a fresh topic
            </label>
            <label className="flex items-center gap-2 text-[11px] text-zinc-400" title="The lanes' Auto: each task starts when the one before it finishes. Left as it is, no lane's mode changes.">
              <input type="checkbox" checked={autoChecked}
                onChange={(e) => { setAutoTouched(true); setAutoMove(e.target.checked) }} className="accent-violet-500" />
              Move on automatically after each task ({targets.length === 1 ? 'lane' : 'lanes'} {targets.join(', ')})
            </label>

            <button
              onClick={runPlan}
              disabled={!intent.trim() || busy}
              className="mt-1 flex items-center justify-center gap-2 rounded-md bg-violet-600 px-3 py-2 text-xs font-medium text-white hover:bg-violet-500 disabled:opacity-40"
            >
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
              {plan ? 'Re-plan' : 'Plan tasks'}
            </button>
            {error && (
              <p className="rounded border border-red-500/30 bg-red-500/10 px-2 py-1.5 text-[11px] text-red-300">
                {error}
              </p>
            )}
          </div>

          {/* ── Plan ── */}
          <div ref={resultRef} className="flex min-w-0 flex-1 flex-col">
            {!plan ? (
              <div className="flex flex-1 items-center justify-center px-8 py-8 text-center">
                <p className="max-w-md text-xs leading-relaxed text-zinc-500">
                  The plan appears here for review before anything is queued. The model reads
                  the agent's real tables and id ranges, so batches cover rows that exist —
                  it never invents a range.
                </p>
              </div>
            ) : (
              <>
                <div className="border-b border-zinc-800 px-4 py-2">
                  {plan.rationale && <p className="text-[11px] text-zinc-400">{plan.rationale}</p>}
                  {plan.facts?.table && (
                    <p className="mt-0.5 text-[10px] text-zinc-600">
                      {plan.facts.table} · {plan.facts.key_min}–{plan.facts.key_max}
                    </p>
                  )}
                  {warnings.map((w, i) => (
                    <p key={i} className="mt-1 flex items-start gap-1.5 text-[11px] text-amber-300">
                      <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{w}
                    </p>
                  ))}
                </div>

                {/* The template: edit once, every task follows. */}
                <details className="border-b border-zinc-800 px-4 py-2" open>
                  <summary className="cursor-pointer text-[11px] font-medium uppercase tracking-wider text-zinc-500">
                    Template — edits apply to every task
                  </summary>
                  <textarea
                    value={template}
                    onChange={(e) => setTemplate(e.target.value)}
                    rows={6}
                    className="mt-2 w-full resize-y rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-zinc-300 focus:border-violet-500/60 focus:outline-none"
                  />
                </details>

                <div className="px-4 py-2 wide:min-h-0 wide:flex-1 wide:overflow-y-auto">
                  <ul className="flex flex-col gap-1.5">
                    {messages.map((m, i) => (
                      <li key={i} className="rounded-md border border-zinc-800 bg-zinc-950/60 p-2">
                        <div className="mb-1 flex items-center justify-between">
                          <span className="text-[10px] font-medium text-zinc-500">
                            Task {i + 1} of {messages.length}
                            {pinned.has(i) && (
                              <span className="ml-1.5 inline-flex items-center gap-0.5 text-violet-400">
                                <Pin className="h-2.5 w-2.5" /> edited — template edits skip this one
                              </span>
                            )}
                          </span>
                          <button onClick={() => dropMessage(i)}
                            className="rounded p-0.5 text-zinc-600 hover:bg-red-500/20 hover:text-red-300">
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </div>
                        <textarea
                          value={m}
                          onChange={(e) => editMessage(i, e.target.value)}
                          rows={3}
                          className="w-full resize-y rounded border border-transparent bg-transparent text-[11px] leading-relaxed text-zinc-300 hover:border-zinc-800 focus:border-violet-500/60 focus:bg-zinc-950 focus:outline-none"
                        />
                      </li>
                    ))}
                  </ul>
                </div>

                <div className="sticky bottom-0 border-t border-zinc-800 bg-zinc-900 px-4 pt-2.5 pb-[max(0.625rem,env(safe-area-inset-bottom))] wide:static">
                  {sent > 0 && <div className="mb-2.5"><Notice tone="emerald">{sentNotice}</Notice></div>}
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="min-w-0 text-[11px] text-zinc-500">
                      {confirmAgain
                        ? `Add the same ${messages.length} task${messages.length === 1 ? '' : 's'} again?`
                        : sent > 0
                          ? ''
                          : `${messages.length} tasks${newSession && newCount ? ` + ${newCount} fresh starts` : ''} → ${distText}`}
                    </span>
                    <div className="ml-auto flex items-center gap-2">
                      {sent > 0 && confirmAgain && (
                        <>
                          <button onClick={() => setConfirmAgain(false)}
                            className="h-11 rounded-xl px-3 text-xs text-zinc-400 hover:bg-zinc-800 wide:h-auto wide:py-1.5">
                            Cancel
                          </button>
                          <button onClick={send} disabled={messages.length === 0 || targets.length === 0}
                            className="flex h-11 items-center gap-1.5 rounded-xl bg-violet-600 px-3 text-xs font-medium text-white hover:bg-violet-500 disabled:opacity-40 wide:h-auto wide:py-1.5">
                            <ListPlus className="h-3.5 w-3.5" />
                            Add again
                          </button>
                        </>
                      )}
                      {sent > 0 && !confirmAgain && (
                        <>
                          <button onClick={() => setConfirmAgain(true)} disabled={messages.length === 0 || targets.length === 0}
                            className="h-11 rounded-xl border border-zinc-700 px-3 text-xs font-medium text-zinc-300 hover:bg-zinc-800 disabled:opacity-40 wide:h-auto wide:py-1.5">
                            Add again
                          </button>
                          <button onClick={onClose}
                            className="h-11 rounded-xl bg-violet-600 px-4 text-xs font-medium text-white hover:bg-violet-500 wide:h-auto wide:py-1.5">
                            Done
                          </button>
                        </>
                      )}
                      {sent === 0 && (
                        <button
                          onClick={send}
                          disabled={messages.length === 0 || targets.length === 0}
                          className="flex h-11 items-center gap-1.5 rounded-xl bg-violet-600 px-4 text-xs font-medium text-white hover:bg-violet-500 disabled:opacity-40 wide:h-auto wide:py-1.5"
                        >
                          <ListPlus className="h-3.5 w-3.5" />
                          Add {messages.length} task{messages.length === 1 ? '' : 's'}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

const inputCls =
  'w-full rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 focus:border-violet-500/60 focus:outline-none'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</label>
      {children}
    </div>
  )
}
