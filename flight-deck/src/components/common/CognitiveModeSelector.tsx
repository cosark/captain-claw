import { useState, useRef, useEffect, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { BrainCog, Check, ChevronDown } from 'lucide-react'
import { MODES } from '../../services/cognitiveModes'

interface Props {
  value: string
  saved: boolean
  onChange: (mode: string) => void
}

export function CognitiveModeSelector({ value, saved, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ top: 0, left: 0, width: 0 })
  const active = MODES.find((m) => m.id === value) || MODES[0]

  // Measure trigger and position dropdown above it
  const updatePos = useCallback(() => {
    if (!triggerRef.current) return
    const r = triggerRef.current.getBoundingClientRect()
    setPos({ top: r.bottom, left: r.left, width: r.width })
  }, [])

  // Close on outside click + reposition on scroll
  useEffect(() => {
    if (!open) return
    updatePos()
    const handleClick = (e: MouseEvent) => {
      const t = e.target as Node
      if (menuRef.current?.contains(t) || triggerRef.current?.contains(t)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', handleClick)
    window.addEventListener('scroll', updatePos, true)
    window.addEventListener('resize', updatePos)
    return () => {
      document.removeEventListener('mousedown', handleClick)
      window.removeEventListener('scroll', updatePos, true)
      window.removeEventListener('resize', updatePos)
    }
  }, [open, updatePos])

  return (
    <div className="mb-3">
      {/* Trigger button */}
      <button
        ref={triggerRef}
        onClick={() => setOpen(!open)}
        className={`w-full flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition-all ${
          open
            ? 'border-violet-500/40 bg-zinc-800/50'
            : saved
              ? 'border-emerald-500/50 bg-emerald-500/5'
              : 'border-zinc-700/50 bg-zinc-950/50 hover:border-zinc-600'
        }`}
      >
        <span className={`h-2.5 w-2.5 rounded-full shrink-0 ${active.color}`}
          style={active.id !== 'neutra' ? { boxShadow: `0 0 6px ${getGlowColor(active.id)}` } : {}} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className={`text-xs font-medium ${active.id !== 'neutra' ? active.textColor : 'text-zinc-400'}`}>
              {active.label}
            </span>
            {saved && <Check className="h-3 w-3 text-emerald-400" />}
          </div>
          {active.id !== 'neutra' && (
            <span className="text-[10px] text-zinc-500 leading-tight">{active.character}</span>
          )}
        </div>
        <ChevronDown className={`h-3 w-3 text-zinc-600 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {/* Portal dropdown — positioned above the trigger */}
      {open && createPortal(
        <div
          ref={menuRef}
          className="fixed rounded-xl border border-zinc-700/50 bg-zinc-900 shadow-2xl shadow-black/60 overflow-hidden max-h-[460px] overflow-y-auto"
          style={{
            top: pos.top + 6,
            left: pos.left,
            width: Math.max(pos.width, 340),
            zIndex: 9999,
          }}
        >
          <div className="px-3 py-2 border-b border-zinc-800 bg-zinc-900/80 sticky top-0 flex items-center gap-1.5">
            <BrainCog className="h-3 w-3 text-zinc-500" />
            <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Cognitive Mode</span>
          </div>
          {MODES.map((mode) => {
            const isActive = mode.id === value
            return (
              <button
                key={mode.id}
                onClick={() => { onChange(mode.id); setOpen(false) }}
                className={`w-full flex items-start gap-2.5 px-3 py-2.5 text-left transition-colors border-b border-zinc-800/40 last:border-b-0 ${
                  isActive ? mode.bgActive : 'hover:bg-zinc-800/60'
                }`}
              >
                <span className={`h-2 w-2 rounded-full shrink-0 mt-1 ${mode.color}`}
                  style={isActive ? { boxShadow: `0 0 6px ${getGlowColor(mode.id)}` } : {}} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className={`text-xs font-medium ${isActive ? mode.textColor : 'text-zinc-300'}`}>
                      {mode.label}
                    </span>
                    <span className="text-[10px] text-zinc-600">{mode.character}</span>
                    {isActive && <Check className="h-3 w-3 text-emerald-400 shrink-0 ml-auto" />}
                  </div>
                  <p className="text-[10px] leading-snug text-zinc-500 mt-0.5">{mode.description}</p>
                </div>
              </button>
            )
          })}
        </div>,
        document.body,
      )}
    </div>
  )
}

function getGlowColor(mode: string): string {
  const map: Record<string, string> = {
    neutra: 'transparent',
    ionian: 'rgba(251, 191, 36, 0.3)',
    dorian: 'rgba(45, 212, 191, 0.3)',
    phrygian: 'rgba(248, 113, 113, 0.3)',
    lydian: 'rgba(167, 139, 250, 0.3)',
    mixolydian: 'rgba(251, 146, 60, 0.3)',
    aeolian: 'rgba(96, 165, 250, 0.3)',
    locrian: 'rgba(232, 121, 249, 0.3)',
  }
  return map[mode] || 'transparent'
}
