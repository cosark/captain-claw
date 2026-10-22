import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { LucideIcon } from 'lucide-react'

// A small action menu, portalled to <body> and placed from its trigger, so a
// card near the bottom of a scrolling column never clips it. Keyboard: arrows,
// Home/End, Enter/Space; Esc closes and hands focus back to the trigger.

export type MenuEntry =
  | { label: string; icon?: LucideIcon; onSelect: () => void; danger?: boolean; disabled?: boolean; title?: string; hidden?: boolean }
  | 'divider'

export function QueueMenu({ anchor, label, items, footer, onClose }: {
  anchor: HTMLElement
  label: string
  items: MenuEntry[]
  footer?: string
  onClose: () => void
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  // Placed once, from where the trigger is when the menu opens; anything
  // that moves the trigger (a scroll, a resize) closes the menu instead.
  // A first guess opens it under the trigger unless the room there is short…
  const [pos] = useState<React.CSSProperties>(() => {
    const r = anchor.getBoundingClientRect()
    const below = window.innerHeight - r.bottom
    const right = Math.max(8, window.innerWidth - r.right)
    return below < 240
      ? { position: 'fixed', right, bottom: window.innerHeight - r.top + 4 }
      : { position: 'fixed', right, top: r.bottom + 4 }
  })
  // …then, before it paints, its real height decides: the side it fits on,
  // else the roomier side with the menu scrolling inside, so the last item
  // (Remove) is never off the screen.
  // (Written straight to the panel's style: `pos` never changes after the
  // first render, so React leaves these alone.)
  useLayoutEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    const r = anchor.getBoundingClientRect()
    const h = panel.scrollHeight
    const spaceBelow = window.innerHeight - r.bottom - 12
    const spaceAbove = r.top - 12
    const down = h <= spaceBelow || (h > spaceAbove && spaceBelow >= spaceAbove)
    panel.style.top = down ? `${r.bottom + 4}px` : ''
    panel.style.bottom = down ? '' : `${window.innerHeight - r.top + 4}px`
    panel.style.maxHeight = `${Math.max(120, down ? spaceBelow : spaceAbove)}px`
  }, [anchor])
  const shown = items.filter((x) => x === 'divider' || !x.hidden)
  // No divider at either end or twice in a row once hidden items are gone.
  const entries: MenuEntry[] = []
  shown.forEach((x, idx) => {
    if (x !== 'divider') { entries.push(x); return }
    const more = shown.slice(idx + 1).some((y) => y !== 'divider')
    if (more && entries.length > 0 && entries[entries.length - 1] !== 'divider') entries.push(x)
  })

  useEffect(() => {
    const first = panelRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not([disabled])')
    first?.focus()
  }, [])

  useEffect(() => {
    // A phone's keyboard going away resizes the window's height: only a
    // change of width (turning the device, a narrower window) closes it.
    const width = window.innerWidth
    const close = () => { if (window.innerWidth !== width) onClose() }
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (panelRef.current?.contains(t) || anchor.contains(t)) return
      onClose()
    }
    // Only a scroll that moves the trigger closes it — not the chat next
    // door streaming in a reply.
    const onScroll = (e: Event) => {
      const t = e.target
      if (t === document || (t instanceof Node && t.contains(anchor))) onClose()
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [anchor, onClose])

  const onKeyDown = (e: React.KeyboardEvent) => {
    const list = Array.from(panelRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not([disabled])') ?? [])
    const at = list.indexOf(document.activeElement as HTMLButtonElement)
    const go = (n: number) => { e.preventDefault(); list[(n + list.length) % list.length]?.focus() }
    if (e.key === 'ArrowDown') go(at + 1)
    else if (e.key === 'ArrowUp') go(at - 1)
    else if (e.key === 'Home') go(0)
    else if (e.key === 'End') go(list.length - 1)
    else if (e.key === 'Escape' || e.key === 'Tab') {
      e.preventDefault()
      e.stopPropagation()
      onClose()
      anchor.focus()
    }
  }

  return createPortal(
    <div
      ref={panelRef}
      role="menu"
      aria-label={label}
      onKeyDown={onKeyDown}
      style={pos}
      className="z-[80] max-h-[calc(100dvh-16px)] min-w-[11rem] max-w-[calc(100vw-16px)] overflow-y-auto overscroll-contain rounded-xl border border-zinc-800 bg-zinc-950 p-1 shadow-xl"
    >
      {entries.map((x, idx) => x === 'divider' ? (
        <div key={`d${idx}`} role="separator" className="my-1 h-px bg-zinc-800" />
      ) : (
        <button
          key={x.label}
          type="button"
          role="menuitem"
          disabled={x.disabled}
          title={x.title}
          onClick={() => {
            onClose()
            x.onSelect()
            // The menu is gone: unless the action put focus somewhere (a
            // card, an editor, the composer), it goes back to the trigger
            // rather than falling to the page.
            requestAnimationFrame(() => {
              const a = document.activeElement as HTMLElement | null
              if ((!a || a === document.body || !a.isConnected) && anchor.isConnected) anchor.focus()
            })
          }}
          className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] transition-colors focus-visible:bg-zinc-800 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:min-h-11 pointer-coarse:py-2.5 pointer-coarse:text-sm ${
            x.danger
              ? 'text-red-600 hover:bg-red-500/10 dark:text-red-400'
              : 'text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100'
          }`}
        >
          {x.icon && <x.icon className="h-3.5 w-3.5 shrink-0 opacity-80" />}
          <span className="min-w-0 flex-1 truncate">{x.label}</span>
        </button>
      ))}
      {footer && <div className="px-2.5 pb-1 pt-1.5 text-[11px] text-zinc-500">{footer}</div>}
    </div>,
    document.body,
  )
}
