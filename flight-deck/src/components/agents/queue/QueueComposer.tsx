import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { Clipboard, ListPlus, Loader2, Maximize2, Minimize2, Paperclip, Play, Plus, type LucideIcon } from 'lucide-react'
import { appendAttachmentRefs, type useFileAttachments } from '../../../hooks/useFileAttachments'
import { touchKeyboard } from '../../../hooks/useMediaQuery'
import { AttachmentChip } from '../AttachmentChip'
import { splitListItems } from './queueModel'
import { FOCUS } from './QueueButton'
import type { QueueCtx } from './QueuePanel'

const tasksN = (n: number) => `${n} task${n === 1 ? '' : 's'}`

/** Where a task added now lands while one is running. Step by step moves on
 *  only when the user marks a task done, so it says that — not "right after". */
function whileRunningHint(m: QueueCtx['model'], auto: boolean): string {
  // A command (the fresh start) finishes by itself, in either mode.
  const ahead = m.k + (m.Dcmd ? 0 : 1)
  if (m.k === 0) return m.Dcmd ? 'Runs right after the current step.' : auto ? 'Runs right after the current task.' : `Runs after you mark task ${m.i} done.`
  return auto
    ? `Runs after the ${tasksN(ahead)} ahead of it.`
    : `Waits behind ${tasksN(ahead)}; each starts when you mark the one before it done.`
}

/** Nothing running and something in the way (the agent away or busy, the
 *  allowance spent): adding asks for a start, so the first waiting task goes
 *  `when` it can — and in Step by step only that one. */
function blockedHint(m: QueueCtx['model'], auto: boolean, when: string): string {
  if (m.k === 0) return `Runs ${when}.`
  return auto
    ? `Runs after the ${tasksN(m.k)} ahead of it, which start ${when}.`
    : `Waits behind ${tasksN(m.k)}. The first starts ${when}; each next one when you mark the one before it done.`
}

// Adding a task: one box, its files, and a hint that says exactly what
// adding will do right now (start at once, wait its turn, wait for the agent).

export function QueueComposer({ ctx, taRef, attachments, panelHeight, closedInfo, quotaSpent, toast }: {
  ctx: QueueCtx
  taRef: RefObject<HTMLTextAreaElement | null>
  attachments: ReturnType<typeof useFileAttachments>
  /** The panel's height: the box grows up to 40% of it (70% when bigger). */
  panelHeight: number
  /** A shared chat that ended (the box takes nothing then). */
  closedInfo: { canRetry: boolean } | null
  quotaSpent: boolean
  toast: ReactNode
}) {
  const closed = !!closedInfo
  const { model, shared, agent, conn, phone } = ctx
  const [draft, setDraft] = useState('')
  const [focused, setFocused] = useState(false)
  const [bigger, setBigger] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const { attachments: atts, addFiles, removeAttachment, clearAttachments, handlePasteEvent, pasteFromClipboard } = attachments

  const pendingUploads = atts.filter((a) => a.status === 'uploading').length
  const uploaded = atts.filter((a) => a.status === 'uploaded' && a.uploadedPath)
  const failed = atts.filter((a) => a.status === 'error').length
  const waitingConn = !conn && atts.some((a) => a.status === 'pending')
  // A file that hasn't uploaded yet would be dropped from the task without a
  // word: Add waits for every one of them.
  const notUploaded = pendingUploads > 0 || atts.some((a) => a.status === 'pending')
  const canAdd = (!!draft.trim() || uploaded.length > 0) && !notUploaded && !closed
  const split = atts.length === 0 && !closed ? splitListItems(draft) : null
  const empty = model.hero.id === 'empty'
  // An idle box under a list folds to one line, so the list keeps the room.
  const collapsed = !empty && !draft && atts.length === 0 && !focused
  const cap = Math.max(64, Math.round(panelHeight * (bigger ? 0.7 : 0.4)))

  useLayoutEffect(() => {
    const ta = taRef.current
    if (!ta) return
    const min = collapsed ? 40 : 64
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(Math.max(ta.scrollHeight, min), cap)}px`
    ta.style.overflowY = ta.scrollHeight > cap ? 'auto' : 'hidden'
  }, [draft, cap, collapsed, taRef])

  const handleAdd = () => {
    if (!canAdd) return
    ctx.act.enqueue(appendAttachmentRefs(draft.trim(), uploaded))
    setDraft('')
    clearAttachments()
    taRef.current?.focus()
  }

  const handleSplit = () => {
    if (!split) return
    for (const item of split) ctx.act.enqueue(item)
    setDraft('')
    ctx.toast(`Added ${split.length} tasks`)
    taRef.current?.focus()
  }

  const placeholder = closed ? 'This chat has ended'
    : empty ? 'Describe a task, e.g. “Find three competitors and compare their pricing”'
    : shared ? 'Describe a task…'
    : 'Add a task — paste or drop files too'

  // What adding does right now — the first that applies, and never more than
  // the queue will do by itself (the hero says what you can do about it).
  const D = model.D
  const running = !D ? '' : model.Dcmd ? 'the current step' : `task ${model.i}`
  const hint: { text: ReactNode; red?: boolean } | null = failed
    ? { text: `${failed} file${failed === 1 ? '' : 's'} didn't upload and won't be included.`, red: true }
    : waitingConn ? { text: `Files upload once ${agent} is connected.` }
    // The card says it ended, and offers Retry when that can help.
    : closedInfo ? (closedInfo.canRetry ? { text: 'You can add tasks once the chat is back.' } : null)
    // (A step running with no task behind it may carry the one you add
    // through, if you ran it with Run now — so no promise then.)
    : ctx.paused ? { text: model.Dcmd && model.k === 0 ? 'The queue is paused.' : `The queue is paused. Tasks wait until you ${model.unstarted ? 'start' : 'resume'} it.` }
    // Held on the running item (an error, a stop, a give-up, or quiet and
    // waiting for you to check it): nothing after it moves until you act.
    : model.held ? { text: `The queue is holding on ${running}: new tasks wait behind it.` }
    : D && !ctx.connected ? { text: `${agent} is offline. New tasks wait behind ${running}.` }
    : D && quotaSpent ? {
      text: model.selfRetry
        ? `${running.charAt(0).toUpperCase()}${running.slice(1)} runs again when today's allowance resets; new tasks follow it.`
        : "Today's allowance is used up. New tasks wait until it resets.",
    }
    : D ? { text: whileRunningHint(model, ctx.auto) }
    : !ctx.connected ? { text: blockedHint(model, ctx.auto, `when ${agent} is back online`) }
    : quotaSpent ? { text: blockedHint(model, ctx.auto, "once today's allowance resets") }
    : ctx.busy ? { text: blockedHint(model, ctx.auto, ctx.stopping ? `once ${agent} has stopped` : `when ${agent} finishes its current reply`) }
    : model.willStart ? {
      text: (
        <>
          Starts right away — {agent} is free.{' '}
          <button
            type="button"
            onClick={() => ctx.act.setPaused(true)}
            title="Pause the queue, add your tasks, then press Start"
            className="rounded font-medium text-violet-700 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/40 dark:text-violet-300"
          >
            Add several first
          </button>
        </>
      ),
    }
    : model.kAll ? { text: `Adding also starts “${model.first}”.` }
    : null

  const attachTitle = conn ? undefined : `Files upload once ${agent} is connected`

  return (
    <div className={`relative shrink-0 border-t border-zinc-800 p-3 ${phone ? 'pb-[max(0.75rem,env(safe-area-inset-bottom))]' : ''}`}>
      {toast}
      <div
        className={`rounded-2xl border border-zinc-800 bg-zinc-900/60 transition-colors focus-within:border-violet-500/50 focus-within:ring-2 focus-within:ring-violet-500/15 ${closed ? 'opacity-60' : ''}`}
        onFocus={() => setFocused(true)}
        onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocused(false) }}
      >
        {!shared && atts.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-2.5 pt-2.5">
            {atts.map((a) => <AttachmentChip key={a.id} attachment={a} onRemove={removeAttachment} />)}
          </div>
        )}
        <textarea
          ref={taRef}
          value={draft}
          rows={collapsed ? 1 : 3}
          disabled={closed}
          aria-label="New task"
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onPaste={shared ? undefined : (e) => {
            // No agent to upload to yet: a pasted file can't come along
            // (text still pastes as usual).
            if (!conn) {
              if (Array.from(e.clipboardData.items).some((it) => it.kind === 'file')) {
                e.preventDefault()
                ctx.toast(`Can't attach files until ${agent} is connected.`)
              }
              return
            }
            handlePasteEvent(e)
          }}
          onKeyDown={(e) => {
            if (e.key !== 'Enter') return
            if (e.altKey && split) { e.preventDefault(); handleSplit(); return }
            // Cmd/Ctrl+Enter adds everywhere; plain Enter only on a real keyboard
            // (an on-screen one starts a new line, as in any messaging app).
            if (e.metaKey || e.ctrlKey || (!e.shiftKey && !touchKeyboard())) {
              e.preventDefault()
              handleAdd()
            }
          }}
          className="block w-full resize-none bg-transparent px-3 pb-1 pt-2.5 text-base leading-relaxed text-zinc-100 placeholder-zinc-500 focus:outline-none disabled:cursor-not-allowed wide:text-[13px]"
        />
        {split && (
          <button
            type="button"
            onMouseDown={(e) => e.preventDefault()}
            onClick={handleSplit}
            title="Each line becomes its own task (Alt+Enter)"
            className="mx-2.5 mb-1 inline-flex items-center gap-1.5 rounded-lg bg-violet-500/10 px-2 py-1 text-[11px] font-medium text-violet-700 hover:bg-violet-500/15 dark:text-violet-300"
          >
            <ListPlus className="h-3.5 w-3.5" />
            Add as {split.length} separate tasks
          </button>
        )}
        <div className="flex items-center gap-0.5 px-1.5 pb-1.5">
          {!shared && (
            <>
              <ToolBtn icon={Paperclip} label="Attach files" title={attachTitle} disabled={!conn || closed} onClick={() => fileInputRef.current?.click()} />
              <ToolBtn
                icon={Clipboard}
                label="Paste from clipboard"
                title={attachTitle}
                disabled={!conn || closed}
                onClick={() => pasteFromClipboard((t) => setDraft((d) => (d ? `${d}\n${t}` : t)))}
              />
            </>
          )}
          <ToolBtn
            icon={bigger ? Minimize2 : Maximize2}
            label={bigger ? 'Smaller editor' : 'Bigger editor'}
            pressed={bigger}
            disabled={closed}
            onClick={() => { setBigger((b) => !b); taRef.current?.focus() }}
          />
          <div className="flex-1" />
          <button
            type="button"
            // Keep the box focused: a click here shouldn't fold it first.
            onMouseDown={(e) => e.preventDefault()}
            onClick={handleAdd}
            disabled={!canAdd}
            title={notUploaded ? 'Waiting for uploads…' : 'Add to queue (Enter)'}
            className={`inline-flex shrink-0 items-center gap-1.5 rounded-xl bg-violet-600 font-medium text-white shadow-sm shadow-violet-900/20 transition-colors hover:bg-violet-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/40 disabled:cursor-not-allowed disabled:opacity-40 ${
              phone ? 'h-10 px-4 text-sm' : 'h-8 px-3 text-xs'
            }`}
          >
            {pendingUploads > 0
              ? <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" />
              : model.willStart ? <Play className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            {pendingUploads > 0 ? 'Uploading…' : (
              <>
                <span className="hidden @3xs:inline">{model.willStart ? 'Add & start' : 'Add to queue'}</span>
                <span className="@3xs:hidden">Add</span>
              </>
            )}
            <kbd className="ml-0.5 hidden rounded border border-white/25 px-1 font-sans text-[10px] font-normal pointer-fine:inline">↵</kbd>
          </button>
        </div>
      </div>
      {!shared && (
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files && e.target.files.length > 0) {
              addFiles(e.target.files)
              e.target.value = ''
              taRef.current?.focus()
            }
          }}
        />
      )}
      {hint && (
        <p className={`mt-1.5 px-1 text-[11px] leading-relaxed ${hint.red ? 'text-red-700 dark:text-red-300' : 'text-zinc-500'}`}>
          {hint.text}
        </p>
      )}
    </div>
  )
}

function ToolBtn({ icon: Icon, label, title, disabled, pressed, onClick }: {
  icon: LucideIcon
  label: string
  title?: string
  disabled?: boolean
  pressed?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-pressed={pressed}
      title={title ?? label}
      className={`inline-flex h-8 w-8 items-center justify-center rounded-lg transition-colors hover:bg-zinc-800 hover:text-zinc-200 disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:h-9 pointer-coarse:w-9 ${FOCUS} ${
        pressed ? 'bg-zinc-800 text-zinc-200' : 'text-zinc-500'
      }`}
    >
      <Icon className="h-4 w-4" />
    </button>
  )
}
