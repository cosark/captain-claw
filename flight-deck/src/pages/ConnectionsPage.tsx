import { useState, type ComponentType, type ReactNode } from 'react'
import { Plug, Mail, LayoutGrid, Sparkles, MessageCircle, Database, Scale, Send } from 'lucide-react'
import GoogleConnection from '../components/connections/GoogleConnection'
import MicrosoftConnection from '../components/connections/MicrosoftConnection'
import CodexConnection from '../components/connections/CodexConnection'
import AntigravityConnection from '../components/connections/AntigravityConnection'
import MCPConnection from '../components/connections/MCPConnection'
import MCPAgentAccess from '../components/connections/MCPAgentAccess'
import TypesenseConnection from '../components/connections/TypesenseConnection'
import WhatsAppNudgeConnection from '../components/connections/WhatsAppNudgeConnection'
import JevConnection from '../components/connections/JevConnection'
import TelegramConnection from '../components/connections/TelegramConnection'
import type { MyTelegram } from '../services/sharedIntegrations'
import { useGoogleAuthStore } from '../stores/googleAuthStore'
import { useMicrosoftAuthStore } from '../stores/microsoftAuthStore'
import { useCodexAuthStore } from '../stores/codexAuthStore'
import { useMCPStore } from '../stores/mcpStore'
import { useJevStore } from '../stores/jevStore'

type TabId = 'google' | 'microsoft' | 'telegram' | 'codex' | 'antigravity' | 'mcp' | 'jev' | 'whatsapp' | 'typesense'

interface TabDef {
  id: TabId
  label: string
  icon: ComponentType<{ className?: string }>
  // Deck setup rather than a person's own connection: left out of the kiosk.
  deckOnly?: boolean
}

const TABS: TabDef[] = [
  { id: 'google', label: 'Google', icon: Mail },
  { id: 'microsoft', label: 'Microsoft', icon: LayoutGrid },
  // The deck's shared bot — a person's own link to it, so the kiosk shows it too.
  { id: 'telegram', label: 'Telegram', icon: Send },
  { id: 'codex', label: 'ChatGPT', icon: Sparkles, deckOnly: true },
  { id: 'antigravity', label: 'Antigravity', icon: Sparkles, deckOnly: true },
  { id: 'mcp', label: 'MCP Servers', icon: Plug },
  // The user's own Jev key (the deck's when sign-in is off) — kiosk users add theirs too.
  { id: 'jev', label: 'Jev', icon: Scale },
  { id: 'whatsapp', label: 'WhatsApp', icon: MessageCircle, deckOnly: true },
  { id: 'typesense', label: 'Deep memory', icon: Database, deckOnly: true },
]

// The last tab this viewer opened — a per-browser convenience only.
const TAB_KEY = 'fd:connections-tab'

function savedTab(): string {
  try { return localStorage.getItem(TAB_KEY) || '' } catch { return '' }
}

function saveTab(id: TabId): void {
  try { localStorage.setItem(TAB_KEY, id) } catch { /* storage blocked: just don't remember */ }
}

// A small dot on the tab for the connections whose state is already loaded:
// green connected, amber configured but not connected (Google / Microsoft) or
// a Jev key saved but not verified.
function useTabDots(telegram: MyTelegram | null): Partial<Record<TabId, 'on' | 'pending'>> {
  const google = useGoogleAuthStore((s) => s.status)
  const microsoft = useMicrosoftAuthStore((s) => s.status)
  const codex = useCodexAuthStore((s) => s.status)
  const mcpServers = useMCPStore((s) => s.servers.length)
  const jev = useJevStore((s) => s.status)
  const dot = (st: { configured?: boolean; connected?: boolean } | null) =>
    st?.connected ? 'on' as const : st?.configured ? 'pending' as const : undefined
  return {
    google: dot(google),
    microsoft: dot(microsoft),
    codex: codex?.connected && !codex.stale ? 'on' : undefined,
    mcp: mcpServers > 0 ? 'on' : undefined,
    jev: jev?.key_set ? (jev.verified === false ? 'pending' : 'on') : undefined,
    telegram: telegram?.link && telegram.link.status !== 'rejected'
      ? (telegram.link.status === 'approved' && telegram.link.linked ? 'on' : 'pending')
      : undefined,
  }
}

// `kiosk` — the locked Simple layout's Connections dialog: a person's own
// connections only (their Google / Microsoft account, their Jev key, the
// deck's MCP servers).
// The ChatGPT subscription and the deep-memory server are deck setup, left to
// the full view; WhatsApp nudges come from Autonomous Work, which the kiosk
// doesn't expose.
export default function ConnectionsPage({ kiosk = false }: { kiosk?: boolean }) {
  const tabs = TABS.filter((t) => !(kiosk && t.deckOnly))
  const [active, setActive] = useState<TabId>(() => {
    const saved = savedTab()
    return (tabs.find((t) => t.id === saved)?.id) ?? tabs[0].id
  })
  const [telegram, setTelegram] = useState<MyTelegram | null>(null)
  const dots = useTabDots(telegram)

  const select = (id: TabId) => {
    setActive(id)
    saveTab(id)
  }

  // Every card stays mounted (inactive tabs are hidden, not removed): a
  // sign-in popup's result listener, a half-typed form and the tab dots all
  // survive switching tabs.
  const panel = (id: TabId, children: ReactNode) =>
    tabs.some((t) => t.id === id) ? (
      <div
        role="tabpanel"
        id={`connections-panel-${id}`}
        aria-labelledby={`connections-tab-${id}`}
        hidden={active !== id}
        className="space-y-4"
      >
        {children}
      </div>
    ) : null

  return (
    <div className="h-full overflow-y-auto bg-zinc-950 text-zinc-200">
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8 pb-16">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-10 w-10 rounded-md bg-zinc-900 border border-zinc-800 flex items-center justify-center">
            <Plug className="h-5 w-5 text-violet-500" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-zinc-100">Connections</h1>
            <p className="text-sm text-zinc-500">
              Link Captain Claw to external services so your agents can read and write on your behalf.
            </p>
          </div>
        </div>

        <div
          role="tablist"
          aria-label="Connections"
          className="flex flex-wrap gap-x-1 border-b border-zinc-800 mb-5"
        >
          {tabs.map((t) => {
            const Icon = t.icon
            const selected = active === t.id
            const dot = dots[t.id]
            return (
              <button
                key={t.id}
                id={`connections-tab-${t.id}`}
                role="tab"
                aria-selected={selected}
                aria-controls={`connections-panel-${t.id}`}
                onClick={() => select(t.id)}
                className={`-mb-px flex items-center gap-2 px-3 py-2.5 text-sm border-b-2 whitespace-nowrap transition-colors ${
                  selected
                    ? 'border-violet-500 text-zinc-100'
                    : 'border-transparent text-zinc-500 hover:text-zinc-300'
                }`}
              >
                <Icon className="h-4 w-4" />
                {t.label}
                {dot && (
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${dot === 'on' ? 'bg-emerald-500' : 'bg-amber-500'}`}
                    title={dot === 'on' ? 'Connected' : 'Not connected yet'}
                  />
                )}
              </button>
            )
          })}
        </div>

        {panel('google', <GoogleConnection />)}
        {panel('microsoft', <MicrosoftConnection />)}
        {panel('telegram', <TelegramConnection onStatus={setTelegram} />)}
        {panel('codex', <CodexConnection />)}
        {panel('antigravity', <AntigravityConnection />)}
        {panel('mcp', <><MCPConnection /><MCPAgentAccess /></>)}
        {panel('jev', <JevConnection />)}
        {panel('whatsapp', <WhatsAppNudgeConnection />)}
        {panel('typesense', <TypesenseConnection />)}
      </div>
    </div>
  )
}
