import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useChatStore } from '../../../stores/chatStore'
import { useQuotaStore } from '../../../stores/quotaStore'
import { queueProgressOf, queueSignalOf, type QueueSignal, type SignalTone } from './queueModel'

// The queue's hooks, kept apart from its components so those stay
// fast-refresh friendly.

// When each lane's running item went quiet: its reply, else the moment the
// queue first saw the agent idle. One record per lane, shared by the panel
// and the badge, so the toggle never says "needs you" while the card still
// says "Wrapping up" (nor the other way round).
const _quiet = new Map<string, { key: string; since: number }>()

/** When this stretch of quiet began (null while there is none). */
export function quietSinceOf(sessionKey: string, key: string | null, repliedAt?: number): number | null {
  if (!key) { _quiet.delete(sessionKey); return null }
  const cur = _quiet.get(sessionKey)
  if (cur && cur.key === key) return cur.since
  const since = repliedAt || Date.now()
  _quiet.set(sessionKey, { key, since })
  return since
}

/** True once the time `at` (ms) has passed; never for 0. One timer, not a
 *  ticking clock — read as an external store, so it is right on first render. */
export function usePassed(at: number): boolean {
  const subscribe = useCallback((done: () => void) => {
    const wait = at > 0 ? at - Date.now() : -1
    if (wait < 0) return () => {}
    const t = setTimeout(done, wait + 1)
    return () => clearTimeout(t)
  }, [at])
  const read = useCallback(() => at > 0 && Date.now() >= at, [at])
  return useSyncExternalStore(subscribe, read, read)
}

const NO_SIGNAL: QueueSignal = { left: 0, tone: 'none', quiet: null, quietSince: 0, settleMs: 0 }

/** Tasks left and whether the queue needs the user — for the queue toggle's
 *  badge and the collapsed rail. A cheap selector: it re-renders only when
 *  one of them changes. A running item that stays quiet past its settle time
 *  needs you too ("Check it", "No reply"), as the card says. */
export function useQueueSignal(sessionKey: string): { left: number; tone: SignalTone } {
  // The allowance decides some cards too (a refusal that retries by itself,
  // the limit card ahead of Parked), as in the panel.
  const quotaSpent = useQuotaStore((q) => !!q.status?.blocked && !q.status.exempt)
  const sig = useChatStore(useShallow((st) => {
    const x = st.sessions.get(sessionKey)
    return x ? queueSignalOf(x, quotaSpent) : NO_SIGNAL
  }))
  const since = quietSinceOf(sessionKey, sig.quiet, sig.quietSince)
  const settled = usePassed(since === null ? 0 : since + sig.settleMs)
  const tone: SignalTone = sig.quiet && settled ? 'needs' : sig.tone
  return useMemo(() => ({ left: sig.left, tone }), [sig.left, tone])
}

const NO_PROGRESS = { done: 0, total: 0, working: false }

export function useQueueProgress(sessionKey: string): { done: number; total: number; working: boolean } {
  return useChatStore(useShallow((st) => {
    const x = st.sessions.get(sessionKey)
    return x ? queueProgressOf(x) : NO_PROGRESS
  }))
}

export interface ToastAction { label: string; run: () => void }
export interface ToastState { id: number; message: string; action?: ToastAction }

const TOAST_MS = 6000
const TOAST_AFTER_HOLD_MS = 3000
let _toastSeq = 0

export function useUndoToast() {
  const [toast, setToast] = useState<ToastState | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const stop = useCallback(() => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
  }, [])
  const arm = useCallback((ms: number) => {
    stop()
    timer.current = setTimeout(() => { timer.current = null; setToast(null) }, ms)
  }, [stop])

  const dismiss = useCallback(() => {
    stop()
    setToast(null)
  }, [stop])

  const show = useCallback((message: string, action?: ToastAction) => {
    setToast({ id: ++_toastSeq, message, action })
    arm(TOAST_MS)
  }, [arm])

  /** The pointer or focus is on the toast: it stays. */
  const hold = stop
  /** …and left it: it goes a little later. */
  const release = useCallback(() => arm(TOAST_AFTER_HOLD_MS), [arm])

  useEffect(() => stop, [stop])

  return { toast, show, dismiss, hold, release }
}
