import { useCallback, useEffect, useState } from 'react'
import { KeyRound, Check, Loader2, Save, ArrowRight, AlertCircle, ArrowDownToLine } from 'lucide-react'
import { useUIStore } from '../../stores/uiStore'
import { useAuthStore } from '../../stores/authStore'
import { useMCPStore, type MCPServer } from '../../stores/mcpStore'
import {
  getOAuthClientConfig,
  getSharedJevKey,
  saveSharedJevKey,
  removeSharedJevKey,
  getSharedSonioxKey,
  saveSharedSonioxKey,
  removeSharedSonioxKey,
  importSonioxEnvKey,
  type OAuthClientConfig,
  type SharedJevKey,
  type SharedSonioxKey,
} from '../../services/sharedIntegrations'

// Admin → Integrations: credentials set once for everyone.
// - Google / Microsoft: the deck has ONE OAuth client each (client id + secret),
//   entered by an admin in Connections; every user connects their own account
//   through it and never sees or types the client. Shown here with a shortcut.
// - Jev: the team key. Users with no key of their own use it; a user's own key
//   wins (captain_claw/flight_deck/jev_routes.py).
// - Soniox: the same, for speech-to-text and voice. The server's SONIOX_API_KEY
//   (the old way) is the last fallback, moved in with one click
//   (captain_claw/flight_deck/soniox_routes.py).
// - MCP servers: private to whoever added them; an admin shares one of their
//   own with everyone here, or takes a shared one back
//   (captain_claw/flight_deck/mcp_storage.py). Adding and editing stay in
//   Connections → MCP Servers.

const CONNECTIONS_TAB_KEY = 'fd:connections-tab'

function OAuthClientRow({ provider, label, refreshTick }: {
  provider: 'google' | 'microsoft'
  label: string
  refreshTick: number
}) {
  const setView = useUIStore((s) => s.setView)
  const [cfg, setCfg] = useState<OAuthClientConfig | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getOAuthClientConfig(provider)
      .then((c) => { setCfg(c); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [provider, refreshTick])

  const shared = !!cfg?.client_id_set && !!cfg?.client_secret_set
  const open = () => {
    try { localStorage.setItem(CONNECTIONS_TAB_KEY, provider) } catch { /* storage blocked */ }
    setView('connections')
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-sm text-zinc-200">{label} OAuth client</div>
        <div className="text-xs text-zinc-500">
          {error ? error
            : !cfg ? 'Loading…'
              : shared ? <>Shared with everyone · client ID <span className="font-mono text-zinc-400">{cfg.client_id ? `${cfg.client_id.slice(0, 12)}…` : ''}</span> · users just press Connect</>
                : 'Not set — users can\'t connect until you add the client ID and secret'}
        </div>
      </div>
      {shared && <Check className="h-4 w-4 text-emerald-500" />}
      <button
        onClick={open}
        className="flex items-center gap-1 rounded-md border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
      >
        {shared ? 'Edit' : 'Set up'} <ArrowRight className="h-3 w-3" />
      </button>
    </div>
  )
}

function JevTeamKey({ refreshTick }: { refreshTick: number }) {
  const [state, setState] = useState<SharedJevKey | null>(null)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      setState(await getSharedJevKey())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])
  useEffect(() => { load() }, [load, refreshTick])

  const run = async (fn: () => Promise<SharedJevKey>, done: string) => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const out = await fn()
      setState(out)
      setNotice(out.warning || done)
      setKey('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (state && !state.available) {
    return (
      <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2 text-xs text-zinc-500">
        Jev: sign-in is off on this deck, so the key in Connections → Jev is already everyone's.
      </div>
    )
  }

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2.5 space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <div className="text-sm text-zinc-200">Jev team key</div>
          <div className="text-xs text-zinc-500">
            {!state ? 'Loading…'
              : state.key_set
                ? <>Shared · <span className="font-mono">{state.key_hint}</span>
                    {state.verified === true && ' · verified'}
                    {state.verify_error === 'refused' && <span className="text-red-400"> · Jev refused it — replace it</span>}
                    {state.verify_error === 'unreachable' && ' · not verified yet'}
                    {' '}· everyone without a key of their own uses it (calls are billed to this key)</>
                : 'Not shared — each user adds their own key in Connections → Jev'}
          </div>
        </div>
        {state?.key_set && state.verified !== false && <Check className="h-4 w-4 text-emerald-500" />}
      </div>
      <div className="flex gap-2">
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder={state?.key_set ? 'Replace the team key' : 'Jev API key from console.typesafe.ai/keys'}
          className="flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-xs font-mono text-zinc-200 focus:border-violet-500/50 focus:outline-none"
        />
        <button
          onClick={() => run(() => saveSharedJevKey(key.trim()), 'Shared with everyone')}
          disabled={busy || !key.trim()}
          className="flex items-center gap-1 rounded-md border border-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
          {state?.key_set ? 'Replace' : 'Share'}
        </button>
        {state?.key_set && (
          <button
            onClick={() => {
              if (confirm('Stop sharing the Jev key? Users without a key of their own lose Jev until they add one.')) {
                run(removeSharedJevKey, 'No longer shared')
              }
            }}
            disabled={busy}
            className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-red-400 disabled:opacity-50"
          >
            Stop sharing
          </button>
        )}
      </div>
      {notice && <div className="text-xs text-emerald-500">{notice}</div>}
      {error && (
        <div className="flex items-start gap-1.5 text-xs text-red-400">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" /> {error}
        </div>
      )}
    </div>
  )
}

function SonioxTeamKey({ refreshTick }: { refreshTick: number }) {
  const [state, setState] = useState<SharedSonioxKey | null>(null)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      setState(await getSharedSonioxKey())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])
  useEffect(() => { load() }, [load, refreshTick])

  const run = async (fn: () => Promise<SharedSonioxKey>, done: string) => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const out = await fn()
      setState(out)
      setNotice(out.warning || done)
      setKey('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (state && !state.available) {
    return (
      <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2 text-xs text-zinc-500">
        Soniox: sign-in is off on this deck, so the key in Connections → Soniox is already everyone's.
      </div>
    )
  }

  // The team key isn't set but the server's environment still has one: every
  // user is on it today, and it can be moved into Flight Deck as is.
  const onEnv = !!state && !state.key_set && state.env_key_set

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2.5 space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <div className="text-sm text-zinc-200">Soniox team key <span className="text-zinc-500">· speech-to-text and voice</span></div>
          <div className="text-xs text-zinc-500">
            {!state ? 'Loading…'
              : state.key_set
                ? <>Shared · <span className="font-mono">{state.key_hint}</span>
                    {state.verified === true && ' · verified'}
                    {state.verify_error === 'refused' && <span className="text-red-400"> · Soniox refused it — replace it</span>}
                    {state.verify_error === 'unreachable' && ' · not verified yet'}
                    {' '}· voice notes, devices and agents of everyone without a key of their own use it
                    {state.env_key_set && <> · the server's SONIOX_API_KEY is no longer needed</>}</>
                : onEnv
                  ? <>Everyone uses the server's SONIOX_API_KEY <span className="font-mono">{state.env_key_hint}</span>. Move it here to manage it in Flight Deck.</>
                  : 'Not shared — each user adds their own key in Connections → Soniox'}
          </div>
        </div>
        {state?.key_set && state.verified !== false && <Check className="h-4 w-4 text-emerald-500" />}
      </div>
      <div className="flex flex-wrap gap-2">
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder={state?.key_set ? 'Replace the team key' : 'Soniox API key from console.soniox.com'}
          className="min-w-0 flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-xs font-mono text-zinc-200 focus:border-violet-500/50 focus:outline-none"
        />
        <button
          onClick={() => run(() => saveSharedSonioxKey(key.trim()), 'Shared with everyone')}
          disabled={busy || !key.trim()}
          className="flex items-center gap-1 rounded-md border border-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
          {state?.key_set ? 'Replace' : 'Share'}
        </button>
        {onEnv && (
          <button
            onClick={() => run(importSonioxEnvKey, "Moved into Flight Deck — you can remove SONIOX_API_KEY from the server's environment")}
            disabled={busy}
            className="flex items-center gap-1 rounded-md border border-violet-500/40 px-3 py-1.5 text-xs font-medium text-violet-300 hover:bg-violet-500/10 disabled:opacity-50"
            title="Share the server's SONIOX_API_KEY as the team key"
          >
            <ArrowDownToLine className="h-3 w-3" /> Move server key here
          </button>
        )}
        {state?.key_set && (
          <button
            onClick={() => {
              const fallback = state.env_key_set
                ? "Users without a key of their own fall back to the server's SONIOX_API_KEY."
                : 'Users without a key of their own lose voice transcription until they add one.'
              if (confirm(`Stop sharing the Soniox key? ${fallback}`)) {
                run(removeSharedSonioxKey, 'No longer shared')
              }
            }}
            disabled={busy}
            className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-red-400 disabled:opacity-50"
          >
            Stop sharing
          </button>
        )}
      </div>
      {notice && <div className="text-xs text-emerald-500">{notice}</div>}
      {error && (
        <div className="flex items-start gap-1.5 text-xs text-red-400">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" /> {error}
        </div>
      )}
    </div>
  )
}

function mcpTarget(s: MCPServer): string {
  return s.transport === 'stdio' ? [s.command, ...(s.args || [])].join(' ') : s.url
}

function SharedMCPServers({ refreshTick }: { refreshTick: number }) {
  const setView = useUIStore((s) => s.setView)
  const authEnabled = useAuthStore((s) => s.authEnabled)
  const { servers, error, refresh, setSharing } = useMCPStore()
  const [busy, setBusy] = useState('')
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    refresh().finally(() => setLoaded(true))
  }, [refresh, refreshTick])

  if (authEnabled === false) {
    return (
      <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2 text-xs text-zinc-500">
        MCP servers: sign-in is off on this deck, so every MCP server is already everyone's.
      </div>
    )
  }

  const shared = servers.filter((s) => s.deck_wide)
  const mine = servers.filter((s) => !s.deck_wide && s.can_use !== false)
  const orphaned = servers.filter((s) => !s.deck_wide && s.can_use === false)

  const open = () => {
    try { localStorage.setItem(CONNECTIONS_TAB_KEY, 'mcp') } catch { /* storage blocked */ }
    setView('connections')
  }

  const toggle = async (s: MCPServer, deckWide: boolean) => {
    const signedIn = s.auth === 'oauth' && s.oauth?.connected
    const question = deckWide
      ? `Share "${s.name}" with everyone on this deck? Every user's agents will be able to use it` +
        (signedIn ? `, acting as ${s.oauth?.connected_by || 'the account signed in'}.` : '.')
      : `Stop sharing "${s.name}"? Only your agents will use it, and other users stop seeing it.`
    if (!confirm(question)) return
    setBusy(s.name)
    try {
      await setSharing(s.name, deckWide)
    } finally {
      setBusy('')
    }
  }

  const row = (s: MCPServer, deckWide: boolean) => (
    <div key={s.name} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-zinc-800/70 pt-2">
      <div className="min-w-0 flex-1">
        <div className="text-xs text-zinc-200">
          {s.name}
          {s.auth === 'oauth' && (
            <span className="text-zinc-500">
              {' '}· {s.oauth?.connected ? `signed in as ${s.oauth.connected_by || 'someone'}` : 'not signed in'}
            </span>
          )}
        </div>
        <div className="text-[11px] text-zinc-500 font-mono truncate">{mcpTarget(s)}</div>
      </div>
      <button
        onClick={() => toggle(s, !deckWide)}
        disabled={!!busy}
        className={
          'flex items-center gap-1 rounded-md border border-zinc-700 px-2.5 py-1 text-xs disabled:opacity-50 ' +
          (deckWide ? 'text-zinc-400 hover:bg-zinc-800 hover:text-red-400' : 'text-zinc-300 hover:bg-zinc-800')
        }
      >
        {busy === s.name && <Loader2 className="h-3 w-3 animate-spin" />}
        {deckWide ? 'Stop sharing' : 'Share with everyone'}
      </button>
    </div>
  )

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2.5 space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <div className="text-sm text-zinc-200">MCP servers</div>
          <div className="text-xs text-zinc-500">
            {!loaded ? 'Loading…'
              : shared.length > 0
                ? <>Shared with everyone · {shared.length} server{shared.length === 1 ? '' : 's'} · every
                    user's agents may use {shared.length === 1 ? 'it' : 'them'} (within each server's allowed agents)</>
                : "Not shared — each user's MCP servers are private to them"}
          </div>
        </div>
        {shared.length > 0 && <Check className="h-4 w-4 text-emerald-500" />}
        <button
          onClick={open}
          className="flex items-center gap-1 rounded-md border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
        >
          Add or edit <ArrowRight className="h-3 w-3" />
        </button>
      </div>
      {shared.map((s) => row(s, true))}
      {mine.length > 0 && (
        <div className="text-[11px] text-zinc-500 pt-1">Yours, private — share one to give it to everyone:</div>
      )}
      {mine.map((s) => row(s, false))}
      {orphaned.length > 0 && (
        <div className="flex items-start gap-1.5 text-xs text-amber-400">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          {orphaned.length} server{orphaned.length === 1 ? '' : 's'} nobody owns any more
          ({orphaned.map((s) => s.name).join(', ')}) — no agent can use {orphaned.length === 1 ? 'it' : 'them'};
          remove {orphaned.length === 1 ? 'it' : 'them'} in Connections.
        </div>
      )}
      {error && (
        <div className="flex items-start gap-1.5 text-xs text-red-400">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" /> {error}
        </div>
      )}
    </div>
  )
}

export function SharedCredentialsAdmin({ refreshTick = 0 }: { refreshTick?: number }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-3">
      <div className="flex items-start gap-3">
        <KeyRound className="h-4 w-4 text-violet-400 mt-0.5" />
        <div>
          <h3 className="text-sm font-semibold text-zinc-200">Shared credentials</h3>
          <p className="text-xs text-zinc-500 mt-0.5">
            Set once here, used by everyone on this deck. Nobody else sees the secrets — users
            only connect their own accounts.
          </p>
        </div>
      </div>
      <OAuthClientRow provider="google" label="Google" refreshTick={refreshTick} />
      <OAuthClientRow provider="microsoft" label="Microsoft" refreshTick={refreshTick} />
      <JevTeamKey refreshTick={refreshTick} />
      <SonioxTeamKey refreshTick={refreshTick} />
      <SharedMCPServers refreshTick={refreshTick} />
    </div>
  )
}
