import { useRef } from 'react'
import { X } from 'lucide-react'
import { FOCUS } from './QueueButton'
import type { ToastState } from './queueHooks'

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)

// One small toast above the queue's composer: what just happened, with an
// Undo (or another follow-up) when it can be taken back. A new one replaces
// the last; it goes by itself after 6 seconds — but not while the pointer is
// on it or focus is inside it (then 3 seconds after they leave).

export function UndoToast({ toast, onDismiss, onHold, onRelease }: {
  toast: ToastState | null
  onDismiss: () => void
  onHold: () => void
  onRelease: () => void
}) {
  return (
    // Always mounted, so a screen reader hears each new message.
    <div role="status" aria-live="polite" className="pointer-events-none absolute inset-x-0 top-0 z-20">
      {/* Keyed: each toast starts unheld (one removed under the pointer or
          the focus fires no leave or blur). */}
      {toast && <ToastCard key={toast.id} toast={toast} onDismiss={onDismiss} onHold={onHold} onRelease={onRelease} />}
    </div>
  )
}

function ToastCard({ toast, onDismiss, onHold, onRelease }: {
  toast: ToastState
  onDismiss: () => void
  onHold: () => void
  onRelease: () => void
}) {
  // Held while either the pointer or focus is on it; released when both left.
  const hover = useRef(false)
  const focus = useRef(false)
  const update = () => { if (hover.current || focus.current) onHold(); else onRelease() }
  return (
    <div
      onMouseEnter={() => { hover.current = true; update() }}
      onMouseLeave={() => { hover.current = false; update() }}
      onFocus={() => { focus.current = true; update() }}
      onBlur={(e) => {
        if (e.currentTarget.contains(e.relatedTarget as Node | null)) return
        focus.current = false
        update()
      }}
      className="pointer-events-auto absolute left-3 right-3 top-0 flex -translate-y-[calc(100%+8px)] items-center gap-2 rounded-xl border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs text-zinc-200 shadow-lg transition-opacity duration-150 starting:opacity-0 motion-reduce:transition-none"
    >
      <span className="min-w-0 flex-1 leading-snug">{toast.message}</span>
      {toast.action && (
        <button
          type="button"
          onClick={() => { const run = toast.action!.run; onDismiss(); run() }}
          title={toast.action.label === 'Undo' ? `Undo (${IS_MAC ? '⌘Z' : 'Ctrl+Z'})` : undefined}
          className={`ml-auto shrink-0 rounded-md px-1.5 py-0.5 font-semibold text-violet-700 hover:bg-violet-500/10 dark:text-violet-300 ${FOCUS}`}
        >
          {toast.action.label}
        </button>
      )}
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className={`shrink-0 rounded-md p-0.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 pointer-coarse:p-2 ${FOCUS}`}
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}
