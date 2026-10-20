import { useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertTriangle,
  BrainCog,
  Check,
  Eye,
  Info,
  LibraryBig,
  Loader2,
  MoreHorizontal,
  RotateCw,
  SlidersHorizontal,
  Tags,
  Target,
  Trash2,
  X,
} from 'lucide-react'
import { IntentionsPanel } from '../agents/IntentionsPanel'
import { TopicsPanel } from '../agents/TopicsPanel'
import { MODES } from '../common/CognitiveModeSelector'
import { VISION_OPTIONS, parseModelFromYaml } from '../common/ModelSelector'
import { getAgentConfig, getAgentMode, setAgentMode, setAgentVision } from '../../services/docker'
import { useContainerStore } from '../../stores/containerStore'
import { useProcessStore } from '../../stores/processStore'

// ── Kiosk: one agent's menu and dialogs ──────────────────────────────
//
// The "⋯" on a kiosk agent row opens what a kiosk user may do with one agent
// of theirs besides chatting and the power switch: look at what it means to
// do and what they talked about (Info: its intentions and topics), change how
// it thinks and whether it sees images itself (Settings), or remove it for
// good, folder and all (Remove).

/** What these dialogs need of an agent row. */
export interface KioskAgent {
  id: string
  kind: 'docker' | 'process' | 'local' | 'shared'
  name: string
  state: string
  reachable: boolean
  host: string
  port: number
  auth: string
  startKey: string
}

export interface AgentMenuItem {
  icon: typeof Info
  label: string
  /** A word more under the label, or why it's off. */
  hint?: string
  onSelect: () => void
  danger?: boolean
  disabled?: boolean
}

// ── The "⋯" menu ──

const MENU_WIDTH = 240

export function AgentMenu({ agentName, items, phone = false, shown = false }: {
  agentName: string
  items: AgentMenuItem[]
  /** Phone row: a 44px touch target, always shown. */
  phone?: boolean
  /** Shown all the time — otherwise only while its row is hovered (or on a touch screen). */
  shown?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number }>({ left: 0, top: 0 })
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  // Under the trigger, right edges aligned — above it when the menu (about
  // this tall) wouldn't fit below.
  const toggle = () => {
    if (open) { setOpen(false); return }
    const t = triggerRef.current?.getBoundingClientRect()
    if (t) {
      const height = items.length * (phone ? 60 : 52) + 8
      const left = Math.max(8, Math.min(t.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - 8))
      setPos(t.bottom + 4 + height > window.innerHeight - 8
        ? { left, bottom: window.innerHeight - t.top + 4 }
        : { left, top: t.bottom + 4 })
    }
    setOpen(true)
  }

  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      const n = e.target as Node
      if (menuRef.current?.contains(n) || triggerRef.current?.contains(n)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      setOpen(false)
    }
    const close = () => setOpen(false)
    document.addEventListener('pointerdown', onDown)
    window.addEventListener('keydown', onKey, true)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', close, true)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      window.removeEventListener('keydown', onKey, true)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', close, true)
    }
  }, [open])

  return (
    <>
      <button
        ref={triggerRef}
        onClick={toggle}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`More for ${agentName}`}
        title={`More — ${agentName}`}
        className={phone
          ? 'flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-zinc-500 transition-colors active:bg-zinc-800'
          : `shrink-0 rounded p-1.5 transition-colors hover:bg-zinc-800 hover:text-zinc-200 focus:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100 ${
            open ? 'bg-zinc-800 text-zinc-200 opacity-100' : `text-zinc-500 ${shown ? '' : 'opacity-0'}`}`}
      >
        <MoreHorizontal className={phone ? 'h-5 w-5' : 'h-3.5 w-3.5'} />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          role="menu"
          aria-label={agentName}
          className="fixed z-50 rounded-xl border border-zinc-700/60 bg-zinc-900 p-1 shadow-2xl shadow-black/40"
          style={{ ...pos, width: MENU_WIDTH }}
        >
          {items.map(({ icon: Icon, label, hint, onSelect, danger, disabled }) => (
            <button
              key={label}
              role="menuitem"
              disabled={disabled}
              onClick={() => { setOpen(false); onSelect() }}
              className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                danger
                  ? 'text-red-600 hover:bg-red-500/10 dark:text-red-400'
                  : 'text-zinc-200 hover:bg-zinc-800'
              } ${phone ? 'py-3' : ''}`}
            >
              <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${danger ? '' : 'text-zinc-400'}`} />
              <span className="min-w-0">
                <span className="block text-sm font-medium">{label}</span>
                {hint && <span className="block text-[11px] leading-snug text-zinc-500">{hint}</span>}
              </span>
            </button>
          ))}
        </div>,
        document.body,
      )}
    </>
  )
}

// ── Dialog shell ──
//
// `wide` (Info): a panel on a larger screen, the whole screen on a phone.
// Otherwise a narrow panel — a bottom sheet on a phone — like kiosk Settings.

function AgentDialog({ title, icon: Icon, agentName, wide = false, onClose, children }: {
  title: string
  icon: typeof Info
  agentName: string
  wide?: boolean
  onClose: () => void
  children: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div
      className={`fixed inset-0 z-50 flex justify-center bg-black/60 wide:items-center wide:p-4 ${wide ? 'items-stretch' : 'items-end'}`}
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label={`${title} — ${agentName}`}
        className={wide
          ? 'relative flex h-[100dvh] w-full flex-col overflow-hidden bg-zinc-950 pb-[env(safe-area-inset-bottom)] shadow-2xl wide:h-[88vh] wide:max-w-5xl wide:rounded-2xl wide:border wide:border-zinc-800 wide:pb-0'
          : 'relative flex max-h-[100dvh] w-full max-w-md flex-col overflow-hidden rounded-t-2xl border border-zinc-800 bg-zinc-950 pb-[env(safe-area-inset-bottom)] shadow-2xl wide:max-h-[88vh] wide:rounded-2xl wide:pb-0'}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-zinc-800 pl-5 pr-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <Icon className="h-4 w-4 shrink-0 text-violet-400" />
            <div className="min-w-0">
              <h2 className="truncate text-sm font-semibold text-zinc-100">{title}</h2>
              <div className="truncate text-[11px] text-zinc-500">{agentName}</div>
            </div>
          </div>
          <button onClick={onClose} title="Close" aria-label="Close" className="rounded-lg p-2 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200">
            <X className="h-5 w-5 wide:h-4 wide:w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  )
}

// ── Info: intentions and topics ──

type InfoTab = 'intentions' | 'topics'
const INFO_TABS: { id: InfoTab; label: string; icon: typeof Info }[] = [
  { id: 'intentions', label: 'Intentions', icon: Target },
  { id: 'topics', label: 'Topics', icon: Tags },
]

export function KioskAgentInfoDialog({ agent, phone, onClose }: { agent: KioskAgent; phone: boolean; onClose: () => void }) {
  const [tab, setTab] = useState<InfoTab>('intentions')
  // Both read the agent itself, through Flight Deck: a stopped one has nothing to say.
  const up = agent.reachable && agent.port > 0

  return (
    <AgentDialog title="Info" icon={Info} agentName={agent.name} wide onClose={onClose}>
      <div className="shrink-0 border-b border-zinc-800 p-2">
        <div className="grid grid-cols-2 rounded-xl bg-zinc-900 p-1 wide:max-w-xs" role="tablist">
          {INFO_TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              role="tab"
              aria-selected={tab === id}
              onClick={() => setTab(id)}
              className={`flex h-9 items-center justify-center gap-1.5 rounded-lg text-sm font-medium transition-colors ${
                tab === id ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300'
              }`}
            >
              <Icon className={`h-4 w-4 ${id === 'intentions' ? 'text-amber-400' : 'text-sky-400'}`} />
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        {!up ? (
          <div className="flex h-full items-center justify-center px-8 text-center text-sm text-zinc-500">
            Power {agent.name} on to see its intentions and topics.
          </div>
        ) : tab === 'intentions' ? (
          <IntentionsPanel key={agent.id} embedded host={agent.host} port={agent.port} auth={agent.auth} agentName={agent.name} />
        ) : (
          <TopicsPanel key={agent.id} embedded phone={phone} host={agent.host} port={agent.port} auth={agent.auth} agentName={agent.name} />
        )}
      </div>
    </AgentDialog>
  )
}

// ── Settings: cognitive mode and vision ──

const VISION_CHOICES = VISION_OPTIONS.map((o) => {
  const [title, detail = ''] = o.label.split(' — ')
  return { value: o.value, title, detail: detail.charAt(0).toUpperCase() + detail.slice(1) }
})

export function KioskAgentSettingsDialog({ agent, restarting, onRestart, onClose }: {
  agent: KioskAgent & { kind: 'docker' | 'process' }
  /** Being restarted right now (or switched on/off). */
  restarting: boolean
  /** Restart it so a start-up setting (vision) applies. Resolves once it's on its way back. */
  onRestart: () => Promise<boolean>
  onClose: () => void
}) {
  const { kind, startKey: key } = agent
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [mode, setMode] = useState('neutra')
  const [modeSaved, setModeSaved] = useState(false)
  const [vision, setVision] = useState('auto')
  const [model, setModel] = useState({ provider: '', model: '' })
  const [saving, setSaving] = useState<'' | 'mode' | 'vision'>('')
  // Vision is read when the agent starts: changed since it last did.
  const [visionPending, setVisionPending] = useState(false)
  const [restarted, setRestarted] = useState(false)
  const running = agent.state === 'running'

  useEffect(() => {
    let live = true
    Promise.all([getAgentMode(kind, key), getAgentConfig(kind, key)])
      .then(([m, cfg]) => {
        if (!live) return
        const info = parseModelFromYaml(cfg.config_yaml || '')
        setMode(m.mode || 'neutra')
        setVision(info.vision)
        setModel({ provider: info.provider, model: info.model })
      })
      .catch((e) => { if (live) setError(`Couldn't load its settings: ${e instanceof Error ? e.message : String(e)}`) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [kind, key])

  // The mode applies on its next reply — no restart.
  const pickMode = async (next: string) => {
    if (next === mode || saving) return
    const was = mode
    setMode(next); setSaving('mode'); setError(''); setModeSaved(false)
    try {
      await setAgentMode(kind, key, next)
      // …and the agent cards' copy of it.
      const cards = kind === 'docker' ? useContainerStore.getState() : useProcessStore.getState()
      cards.setCognitiveMode(key, next)
      setModeSaved(true)
      setTimeout(() => setModeSaved(false), 2000)
    } catch (e) {
      setMode(was)
      setError(`Couldn't change how it thinks: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving('')
    }
  }

  const pickVision = async (next: string) => {
    if (next === vision || saving) return
    const was = vision
    setVision(next); setSaving('vision'); setError('')
    try {
      await setAgentVision(kind, key, next)
      setVisionPending(true)
      setRestarted(false)
    } catch (e) {
      setVision(was)
      setError(`Couldn't change vision: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving('')
    }
  }

  const restart = async () => {
    if (await onRestart()) { setVisionPending(false); setRestarted(true) }
  }

  const textOnly = model.provider === 'antigravity-cli'
  const current = MODES.find((m) => m.id === mode) ?? MODES[0]

  return (
    <AgentDialog title="Agent settings" icon={SlidersHorizontal} agentName={agent.name} onClose={onClose}>
      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto overscroll-contain p-5">
        {loading ? (
          <div className="flex items-center justify-center py-10 text-sm text-zinc-500">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
          </div>
        ) : (
          <>
            <section>
              <SectionTitle icon={BrainCog} title="How it thinks" saving={saving === 'mode'} saved={modeSaved} />
              <p className="mb-2.5 text-[11px] leading-relaxed text-zinc-500">
                Its cognitive mode — the way it reasons. Applies from its next reply.
              </p>
              <div role="radiogroup" aria-label="Cognitive mode" className="grid grid-cols-2 gap-1.5">
                {MODES.map((m) => {
                  const on = m.id === mode
                  return (
                    <button
                      key={m.id}
                      role="radio"
                      aria-checked={on}
                      onClick={() => pickMode(m.id)}
                      disabled={!!saving}
                      className={`flex min-w-0 items-start gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors disabled:cursor-wait ${
                        on ? `border-violet-500/50 ${m.bgActive}` : 'border-zinc-800 hover:border-zinc-700 hover:bg-zinc-900'
                      }`}
                    >
                      <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${m.color}`} />
                      <span className="min-w-0">
                        <span className={`block truncate text-xs font-medium ${on ? m.textColor : 'text-zinc-200'}`}>{m.label}</span>
                        <span className="block truncate text-[10px] text-zinc-500">{m.character}</span>
                      </span>
                    </button>
                  )
                })}
              </div>
              <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">{current.description}</p>
            </section>

            <section>
              <SectionTitle icon={Eye} title="Vision" saving={saving === 'vision'} />
              <p className="mb-2.5 text-[11px] leading-relaxed text-zinc-500">
                Whether its own model looks at the pictures you send.
                {model.provider && <> It runs on <span className="text-zinc-400">{model.provider}{model.model ? ` / ${model.model}` : ''}</span>.</>}
              </p>
              {textOnly ? (
                <p className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2.5 text-xs text-zinc-400">
                  This model reads text only.
                </p>
              ) : (
                <div role="radiogroup" aria-label="Vision" className="space-y-1.5">
                  {VISION_CHOICES.map((o) => {
                    const on = o.value === vision
                    return (
                      <button
                        key={o.value}
                        role="radio"
                        aria-checked={on}
                        onClick={() => pickVision(o.value)}
                        disabled={!!saving}
                        className={`flex w-full items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors disabled:cursor-wait ${
                          on ? 'border-violet-500/50 bg-violet-500/10' : 'border-zinc-800 hover:border-zinc-700 hover:bg-zinc-900'
                        }`}
                      >
                        <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${on ? 'border-violet-400 bg-violet-500' : 'border-zinc-600'}`}>
                          {on && <span className="h-1.5 w-1.5 rounded-full bg-white" />}
                        </span>
                        <span className="min-w-0">
                          <span className="block text-xs font-medium text-zinc-200">{o.title}</span>
                          <span className="block text-[11px] text-zinc-500">{o.detail}</span>
                        </span>
                      </button>
                    )
                  })}
                </div>
              )}
              {visionPending && (
                <div className="mt-2.5 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
                  <span className="min-w-0 flex-1">
                    {running ? 'Saved. It applies once the agent restarts.' : 'Saved. It applies the next time the agent starts.'}
                  </span>
                  {running && (
                    <button
                      onClick={restart}
                      disabled={restarting}
                      className="flex shrink-0 items-center gap-1 rounded-md bg-amber-500/20 px-2 py-1 font-medium hover:bg-amber-500/30 disabled:opacity-60"
                    >
                      {restarting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
                      Restart now
                    </button>
                  )}
                </div>
              )}
              {restarted && (
                <p className="mt-2.5 flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-400">
                  <Check className="h-3.5 w-3.5" /> Restarting — it's back in a moment.
                </p>
              )}
            </section>
          </>
        )}
        {error && (
          <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {error}
          </div>
        )}
      </div>
    </AgentDialog>
  )
}

function SectionTitle({ icon: Icon, title, saving, saved = false }: { icon: typeof Info; title: string; saving: boolean; saved?: boolean }) {
  return (
    <h3 className="mb-1 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400">
      <Icon className="h-3.5 w-3.5" />
      {title}
      {saving && <Loader2 className="h-3 w-3 animate-spin text-zinc-500" />}
      {saved && <span className="flex items-center gap-0.5 normal-case tracking-normal text-emerald-600 dark:text-emerald-400"><Check className="h-3 w-3" />saved</span>}
    </h3>
  )
}

// ── Remove ──

export function KioskRemoveAgentDialog({ agent, busy, onRemove, onOpenLibrary, onClose }: {
  agent: KioskAgent
  /** At work right now (in a chat open here). */
  busy: boolean
  /** Removes it; rejects with what went wrong. */
  onRemove: () => Promise<void>
  /** Opens the File library on its files — to keep some before they go. */
  onOpenLibrary?: () => void
  onClose: () => void
}) {
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState('')
  const cancelRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { cancelRef.current?.focus() }, [])

  const remove = async () => {
    setRemoving(true); setError('')
    try {
      await onRemove()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setRemoving(false)
    }
  }

  return (
    <AgentDialog title="Remove agent" icon={Trash2} agentName={agent.name} onClose={removing ? () => {} : onClose}>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain p-5">
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-red-500/10 text-red-600 dark:text-red-400">
            <AlertTriangle className="h-[18px] w-[18px]" />
          </span>
          <div className="min-w-0 text-sm text-zinc-300">
            <p className="font-medium text-zinc-100">Remove {agent.name} for good?</p>
            <p className="mt-1 text-zinc-400">
              {busy ? 'It is working right now — that stops. ' : ''}
              It is switched off and deleted from this deck, with everything it keeps:
            </p>
            <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[13px] text-zinc-400">
              <li>its conversations and what it remembers</li>
              <li>its files and datastore tables</li>
              <li>its settings, and the scheduled tasks that run it</li>
            </ul>
            <p className="mt-2 font-medium text-red-600 dark:text-red-400">This can't be undone.</p>
          </div>
        </div>
        {onOpenLibrary && (
          <button
            onClick={onOpenLibrary}
            disabled={removing}
            className="flex w-full items-center gap-2.5 rounded-xl border border-zinc-800 bg-zinc-900/60 px-3.5 py-2.5 text-left text-xs text-zinc-400 transition-colors hover:border-violet-500/40 hover:bg-zinc-800/60 disabled:opacity-50"
          >
            <LibraryBig className="h-4 w-4 shrink-0 text-violet-600 dark:text-violet-400" />
            <span>Want to keep a file? Download it from the <span className="font-medium text-zinc-200">File library</span> first.</span>
          </button>
        )}
        {error && (
          <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> Couldn't remove it: {error}
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center justify-end gap-2 border-t border-zinc-800 px-5 py-3.5">
        <button
          ref={cancelRef}
          onClick={onClose}
          disabled={removing}
          className="rounded-lg px-3.5 py-2 text-sm font-medium text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={remove}
          disabled={removing}
          className="flex items-center gap-1.5 rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-500 disabled:opacity-60"
        >
          {removing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
          {removing ? 'Removing…' : 'Remove agent'}
        </button>
      </div>
    </AgentDialog>
  )
}
