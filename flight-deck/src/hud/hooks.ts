// Small React hooks shared by every HUD screen.

import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, MouseEvent, RefObject } from 'react'
import { IS_GLASSES } from './device'
import { claimActivation, focusInitial } from './focus'

export interface ActivateProps {
  role: 'button'
  tabIndex: 0 | -1
  'aria-disabled'?: true
  onClick: (e: MouseEvent<HTMLElement>) => void
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => void
}

/**
 * Props that make any element a D-pad button: focusable, activated by a
 * pinch / Enter / Space / click — exactly once per pinch (see claimActivation).
 * Spread onto a <div>: `<div className="hud-btn" {...useActivate(fn)}>`.
 */
export function useActivate(fn: (() => void) | null | undefined, opts?: { disabled?: boolean }): ActivateProps {
  const ref = useRef(fn)
  useEffect(() => { ref.current = fn })
  const disabled = !!opts?.disabled || !fn
  const run = useCallback(() => {
    if (disabled || !ref.current) return
    if (!claimActivation()) return
    ref.current()
  }, [disabled])
  return {
    role: 'button',
    tabIndex: disabled ? -1 : 0,
    ...(disabled ? { 'aria-disabled': true as const } : {}),
    onClick: (e) => { e.preventDefault(); run() },
    onKeyDown: (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        e.stopPropagation()
        run()
      }
    },
  }
}

/**
 * Focus the screen's first stop (or the one Back should restore) once its
 * content is ready. Never steals focus that is already on this screen —
 * except right after Back, where the host's reset to the first control must
 * not beat the restore (see focusInitial).
 */
export function useAutoFocus(ready: boolean): void {
  useEffect(() => {
    if (!ready) return
    // Two frames: let React commit and the browser lay out first.
    let raf2 = 0
    const raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => { focusInitial() })
    })
    return () => { cancelAnimationFrame(raf1); cancelAnimationFrame(raf2) }
  }, [ready])
}

/** A swipe: at least this far sideways, twice as far as up / down, this quick. */
const SWIPE_PX = 64
const SWIPE_MS = 600

/**
 * A sideways finger swipe on `ref`'s element calls `onSwipe(1)` (right to
 * left: next) or `onSwipe(-1)` (left to right: previous) — the touch
 * counterpart of the Right / Left keys. Passive listeners that never cancel
 * anything, so vertical scrolling is untouched. Never on glasses.
 */
export function useHorizontalSwipe(ref: RefObject<HTMLElement | null>, onSwipe: (dir: 1 | -1) => void, enabled = true): void {
  const fn = useRef(onSwipe)
  useEffect(() => { fn.current = onSwipe })
  useEffect(() => {
    const el = ref.current
    if (!el || !enabled || IS_GLASSES) return
    let start: { x: number; y: number; at: number } | null = null
    const onStart = (e: TouchEvent) => {
      const t = e.touches[0]
      start = e.touches.length === 1 && t ? { x: t.clientX, y: t.clientY, at: Date.now() } : null
    }
    const onEnd = (e: TouchEvent) => {
      const s = start
      start = null
      const t = e.changedTouches[0]
      if (!s || !t || e.touches.length) return
      const dx = t.clientX - s.x
      const dy = t.clientY - s.y
      if (Date.now() - s.at > SWIPE_MS || Math.abs(dx) < SWIPE_PX || Math.abs(dx) < 2 * Math.abs(dy)) return
      fn.current(dx < 0 ? 1 : -1)
    }
    const onCancel = () => { start = null }
    el.addEventListener('touchstart', onStart, { passive: true })
    el.addEventListener('touchend', onEnd, { passive: true })
    el.addEventListener('touchcancel', onCancel, { passive: true })
    return () => {
      el.removeEventListener('touchstart', onStart)
      el.removeEventListener('touchend', onEnd)
      el.removeEventListener('touchcancel', onCancel)
    }
  }, [ref, enabled])
}

/** Current time, refreshed every `ms` (header clock, relative times). */
export function useNow(ms = 15_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(id)
  }, [ms])
  return now
}
