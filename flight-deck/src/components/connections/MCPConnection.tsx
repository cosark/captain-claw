import { useEffect, useState } from 'react'
import {
  Plug,
  Loader2,
  Check,
  AlertCircle,
  RefreshCw,
  Trash2,
  ChevronDown,
  Plus,
  X,
  Link as LinkIcon,
  Terminal,
  Pencil,
  Users,
  Globe,
  LogIn,
  LogOut,
} from 'lucide-react'
import {
  openSignInPopup,
  useMCPStore,
  type MCPServer,
  type MCPServerInput,
  type MCPProbeResult,
  type MCPTransport,
} from '../../stores/mcpStore'
import { useAuthStore } from '../../stores/authStore'
import { AgentAllowlistPicker } from './AgentAllowlistPicker'

const SECRET_PLACEHOLDER = '••••••••'

// How an http server authenticates. 'client_credentials' and 'none' are both
// stored as auth '' (client_credentials = client_id + token_endpoint set).
type AuthChoice = 'none' | 'client_credentials' | 'oauth'

const AUTH_LABELS: Record<AuthChoice, string> = {
  none: 'None',
  client_credentials: 'Client credentials',
  oauth: 'OAuth sign-in',
}

function authChoiceOf(server?: MCPServer): AuthChoice {
  if (!server) return 'none'
  if (server.auth === 'oauth') return 'oauth'
  return server.client_id ? 'client_credentials' : 'none'
}

function probeText(probe: MCPProbeResult): string {
  if (probe.ok && probe.needs_sign_in) {
    return `Sign-in ready — ${probe.issuer || 'the authorization server'} ${
      probe.registration_supported
        ? 'supports dynamic client registration'
        : 'will use the client ID you entered'
    }. Save, then sign in.`
  }
  if (!probe.ok) return `Probe failed: ${probe.error}`
  const n = probe.tools_count ?? 0
  return `Probe ok — ${n} tool${n === 1 ? '' : 's'}`
}

// A stdio server is a command FD spawns on its host, so the backend lets only
// an admin add / edit / test / remove one. With auth off the single local user
// is the admin.
function useCanManageStdio(): boolean {
  return useAuthStore((s) => s.authEnabled === false || s.user?.role === 'admin')
}

// Only an admin on an auth-enabled deck can share a server with every user;
// with auth off there is one user and nothing to share.
function useCanShareDeckWide(): boolean {
  return useAuthStore((s) => s.authEnabled === true && s.user?.role === 'admin')
}

function useOwnershipShown(): boolean {
  return useAuthStore((s) => s.authEnabled === true)
}

function emptyForm(): MCPServerInput {
  return {
    name: '',
    transport: 'http',
    url: '',
    auth: '',
    scope: '',
    client_id: '',
    client_secret: '',
    token_endpoint: '',
    headers: {},
    enabled: true,
    allowed_agents: [],
    deck_wide: false,
    command: '',
    args: [],
    env: {},
  }
}

function fromServer(server: MCPServer): MCPServerInput {
  return {
    name: server.name,
    transport: server.transport || 'http',
    url: server.url || '',
    auth: server.auth || '',
    scope: server.scope || '',
    client_id: server.client_id || '',
    client_secret: server.client_secret_set ? SECRET_PLACEHOLDER : '',
    token_endpoint: server.token_endpoint || '',
    headers: server.headers || {},
    enabled: server.enabled !== false,
    allowed_agents: server.allowed_agents || [],
    deck_wide: !!server.deck_wide,
    command: server.command || '',
    args: server.args || [],
    env: server.env || {},
  }
}

interface ServerRowProps {
  server: MCPServer
  onEdit: () => void
  // Shown, but not editable/testable/removable: a stdio server seen by a
  // non-admin, or a deck-wide server seen by a non-admin.
  readOnly?: boolean
}

function ServerRow({ server, onEdit, readOnly = false }: ServerRowProps) {
  const { testing, lastTestResult, testServer, removeServer, signIn, signOut } = useMCPStore()
  const [confirming, setConfirming] = useState(false)
  const isTesting = !!testing[server.name]
  const result = lastTestResult[server.name]
  const transport: MCPTransport = server.transport || 'http'
  const allowed = server.allowed_agents || []
  const isOAuth = transport === 'http' && server.auth === 'oauth'
  const signedIn = isOAuth && !!server.oauth?.connected
  const ownershipShown = useOwnershipShown()
  const deckWide = !!server.deck_wide
  // An admin sees an ownerless private server (an OAuth sign-in whose user
  // is gone) only to remove it: nothing can call it.
  const orphaned = server.can_use === false

  const statusPill = isOAuth && !signedIn ? (
    <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border bg-amber-500/15 text-amber-400 border-amber-500/30">
      <AlertCircle className="h-3 w-3" /> Sign-in needed
    </span>
  ) : server.initialized ? (
    <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border bg-emerald-500/15 text-emerald-500 border-emerald-500/30">
      <Check className="h-3 w-3" /> Connected
    </span>
  ) : server.last_error ? (
    <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border bg-red-500/15 text-red-500 border-red-500/30">
      <AlertCircle className="h-3 w-3" /> Error
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border bg-zinc-500/15 text-zinc-400 border-zinc-500/30">
      Not initialized
    </span>
  )

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-zinc-100">{server.name}</span>
            {statusPill}
            <span
              className={
                'inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border ' +
                (transport === 'stdio'
                  ? 'bg-violet-500/10 text-violet-300 border-violet-500/30'
                  : 'bg-blue-500/10 text-blue-400 border-blue-500/30')
              }
            >
              {transport === 'stdio' ? (
                <Terminal className="h-3 w-3" />
              ) : (
                <Globe className="h-3 w-3" />
              )}
              {transport}
            </span>
            {isOAuth ? (
              <span className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border bg-blue-500/10 text-blue-400 border-blue-500/30">
                OAuth sign-in
              </span>
            ) : transport === 'http' && server.client_id && (
              <span className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border bg-blue-500/10 text-blue-400 border-blue-500/30">
                OAuth2
              </span>
            )}
            {!server.enabled && (
              <span className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border bg-amber-500/10 text-amber-400 border-amber-500/30">
                Disabled
              </span>
            )}
            {ownershipShown && (
              <span
                className={
                  'inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border ' +
                  (deckWide
                    ? 'bg-amber-500/10 text-amber-400 border-amber-500/30'
                    : 'bg-zinc-500/10 text-zinc-300 border-zinc-500/30')
                }
                title={
                  deckWide
                    ? "Shared by an admin: every user's agents may use this server"
                    : 'Private: only your agents can use it, and only you see it'
                }
              >
                {deckWide ? 'Deck-wide' : 'Private'}
              </span>
            )}
            <span
              className={
                'inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border ' +
                (allowed.length === 0
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                  : 'bg-zinc-500/10 text-zinc-300 border-zinc-500/30')
              }
              title={
                allowed.length === 0
                  ? ownershipShown && !deckWide
                    ? 'Every one of your agents may use this server'
                    : 'Every agent in the fleet may use this server'
                  : `Allowed: ${allowed.join(', ')}`
              }
            >
              <Users className="h-3 w-3" />
              {allowed.length === 0
                ? ownershipShown && !deckWide ? 'All your agents' : 'All agents'
                : `${allowed.length} agent${allowed.length === 1 ? '' : 's'}`}
            </span>
          </div>
          {transport === 'http' ? (
            <div className="text-xs text-zinc-500 mt-1 break-all">
              <LinkIcon className="inline h-3 w-3 mr-1 -mt-0.5" />
              {server.url}
            </div>
          ) : (
            <div className="text-xs text-zinc-500 mt-1 break-all font-mono">
              <Terminal className="inline h-3 w-3 mr-1 -mt-0.5" />
              {server.command}
              {(server.args && server.args.length > 0) ? ' ' + server.args.join(' ') : ''}
            </div>
          )}
          {orphaned && (
            <div className="text-xs text-red-400 mt-1">
              Nobody owns this server any more, so no agent can use it — remove it, and
              have its user add it again.
            </div>
          )}
          {signedIn && !orphaned && (
            <div className="text-xs text-emerald-400 mt-1 break-all">
              Signed in{server.oauth?.connected_by ? ` by ${server.oauth.connected_by}` : ''}
              {server.oauth?.issuer ? ` · ${server.oauth.issuer}` : ''}
            </div>
          )}
          {allowed.length > 0 && (
            <div className="text-[11px] text-zinc-500 mt-1 flex flex-wrap gap-1">
              {allowed.map((slug) => (
                <span
                  key={slug}
                  className="rounded bg-zinc-800/80 px-1.5 py-0.5 font-mono text-zinc-300"
                >
                  {slug}
                </span>
              ))}
            </div>
          )}
          {typeof server.tools_count === 'number' && server.tools_count > 0 && (
            <div className="text-xs text-zinc-400 mt-1">
              {server.tools_count} tool{server.tools_count === 1 ? '' : 's'} discovered
            </div>
          )}
          {server.last_error && (
            <div className="text-xs text-red-400 mt-1 break-all">
              {server.last_error}
            </div>
          )}
          {result && (
            <div
              className={
                'text-xs mt-1 break-all ' +
                (result.ok ? 'text-emerald-400' : 'text-red-400')
              }
            >
              {probeText(result)}
            </div>
          )}
        </div>
        {readOnly ? (
          <span
            className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border bg-zinc-500/10 text-zinc-400 border-zinc-500/30 shrink-0"
            title={
              deckWide
                ? 'An admin shared this server with the whole deck — only an admin can change it'
                : 'stdio servers run a command on the Flight Deck host — only an admin can change them'
            }
          >
            Admin-managed
          </span>
        ) : (
          <div className="flex items-center gap-1.5 flex-wrap shrink-0">
            {isOAuth && !orphaned && (
              <button
                type="button"
                onClick={() => signIn(server.name)}
                className={
                  'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs ' +
                  (signedIn
                    ? 'border border-zinc-700 hover:bg-zinc-800 text-zinc-200'
                    : 'bg-violet-600 hover:bg-violet-500 text-white')
                }
                title="Sign Flight Deck in to this server in a popup (OAuth with dynamic client registration)"
              >
                <LogIn className="h-3.5 w-3.5" />
                {signedIn ? 'Reconnect' : 'Sign in'}
              </button>
            )}
            {signedIn && (
              <button
                type="button"
                onClick={() => signOut(server.name)}
                className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 hover:bg-zinc-800 px-2.5 py-1.5 text-xs text-zinc-200"
                title="Forget this server's tokens (and revoke them, when the server supports it)"
              >
                <LogOut className="h-3.5 w-3.5" />
                Sign out
              </button>
            )}
            <button
              type="button"
              onClick={onEdit}
              className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 hover:bg-zinc-800 px-2.5 py-1.5 text-xs text-zinc-200"
              title="Edit this server"
            >
              <Pencil className="h-3.5 w-3.5" />
              Edit
            </button>
            {!orphaned && (
              <button
                type="button"
                onClick={() => testServer(server.name)}
                disabled={isTesting}
                className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 hover:bg-zinc-800 px-2.5 py-1.5 text-xs text-zinc-200 disabled:opacity-50"
                title="Run an end-to-end probe (initialize + list tools)"
              >
                {isTesting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="h-3.5 w-3.5" />
                )}
                Test
              </button>
            )}
            {confirming ? (
              <>
                <button
                  type="button"
                  onClick={async () => {
                    await removeServer(server.name)
                    setConfirming(false)
                  }}
                  className="inline-flex items-center gap-1.5 rounded-md bg-red-600 hover:bg-red-500 px-2.5 py-1.5 text-xs text-white"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Confirm
                </button>
                <button
                  type="button"
                  onClick={() => setConfirming(false)}
                  className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 hover:bg-zinc-800 px-2.5 py-1.5 text-xs text-zinc-300"
                >
                  <X className="h-3.5 w-3.5" />
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setConfirming(true)}
                className="inline-flex items-center gap-1.5 rounded-md border border-red-500/30 hover:bg-red-500/10 px-2.5 py-1.5 text-xs text-red-400"
                title="Remove this server"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Remove
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

interface ServerFormProps {
  initial?: MCPServer
  onClose: () => void
  // false for a non-admin: the stdio transport tab is not offered.
  allowStdio?: boolean
}

function ServerForm({ initial, onClose, allowStdio = true }: ServerFormProps) {
  const { saving, saveServer, probeTransient, signIn } = useMCPStore()
  const [form, setForm] = useState<MCPServerInput>(
    initial ? fromServer(initial) : emptyForm(),
  )
  const [authChoice, setAuthChoice] = useState<AuthChoice>(authChoiceOf(initial))
  const canShareDeckWide = useCanShareDeckWide()
  const [probing, setProbing] = useState(false)
  const [probe, setProbe] = useState<MCPProbeResult | null>(null)

  // stdio args is an array — render it as a single-line space-separated input
  // and split on save. Same for env (KEY=VALUE per line).
  const [argsText, setArgsText] = useState<string>(
    (form.args || []).join(' '),
  )
  const [envText, setEnvText] = useState<string>(
    Object.entries(form.env || {})
      .map(([k, v]) => `${k}=${v}`)
      .join('\n'),
  )

  const transport: MCPTransport = form.transport || 'http'
  const isEdit = !!initial
  const transportOptions: MCPTransport[] = allowStdio ? ['http', 'stdio'] : ['http']

  const valid = (() => {
    if (!form.name.trim()) return false
    if (transport === 'http') return !!(form.url || '').trim()
    if (transport === 'stdio') return !!(form.command || '').trim()
    return false
  })()

  function update<K extends keyof MCPServerInput>(key: K, value: MCPServerInput[K]) {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  function buildPayload(): MCPServerInput {
    const payload: MCPServerInput = {
      name: form.name.trim(),
      transport,
      enabled: form.enabled !== false,
      allowed_agents: (form.allowed_agents || []).map((s) => s.trim()).filter(Boolean),
      headers: form.headers || {},
    }
    // Non-admins leave it out: the server keeps its setting (private).
    if (canShareDeckWide) payload.deck_wide = !!form.deck_wide
    if (transport === 'http') {
      payload.url = (form.url || '').trim()
      payload.auth = authChoice === 'oauth' ? 'oauth' : ''
      payload.scope = authChoice === 'oauth' ? (form.scope || '').trim() : ''
      if (authChoice === 'none') {
        payload.client_id = ''
        payload.client_secret = ''
        payload.token_endpoint = ''
      } else {
        payload.client_id = (form.client_id || '').trim()
        // SECRET_PLACEHOLDER means "keep what was already stored".
        payload.client_secret =
          form.client_secret === SECRET_PLACEHOLDER ? '' : form.client_secret || ''
        payload.token_endpoint =
          authChoice === 'client_credentials' ? (form.token_endpoint || '').trim() : ''
      }
    } else {
      payload.command = (form.command || '').trim()
      payload.args = argsText.trim()
        ? argsText.trim().split(/\s+/)
        : []
      const envObj: Record<string, string> = {}
      for (const line of envText.split('\n')) {
        const trimmed = line.trim()
        if (!trimmed || trimmed.startsWith('#')) continue
        const eq = trimmed.indexOf('=')
        if (eq <= 0) continue
        envObj[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1)
      }
      payload.env = envObj
    }
    return payload
  }

  async function onProbe() {
    if (!valid) return
    setProbing(true)
    setProbe(null)
    try {
      const result = await probeTransient(buildPayload())
      setProbe(result)
    } finally {
      setProbing(false)
    }
  }

  // An OAuth server nobody has signed in to yet is saved and signed in to in
  // one click; the popup must open now, before any await, or it's blocked.
  const signInOnSave =
    transport === 'http' && authChoice === 'oauth' && !initial?.oauth?.connected

  async function onSave() {
    if (!valid) return
    const popup = signInOnSave ? openSignInPopup() : null
    const saved = await saveServer(buildPayload())
    if (!saved) {
      popup?.close()
      return
    }
    onClose()
    if (signInOnSave) await signIn(saved.name, popup)
  }

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950/70 px-4 py-3 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-zinc-100">
          {isEdit ? `Edit MCP server — ${initial!.name}` : 'Add MCP server'}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="text-zinc-500 hover:text-zinc-300"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Transport tabs */}
      <div className="inline-flex rounded-md border border-zinc-800 bg-zinc-900 p-0.5">
        {transportOptions.map((t) => (
          <button
            key={t}
            type="button"
            disabled={isEdit}
            onClick={() => update('transport', t)}
            className={
              'inline-flex items-center gap-1.5 rounded px-3 py-1 text-xs ' +
              (transport === t
                ? 'bg-violet-600 text-white'
                : 'text-zinc-400 hover:text-zinc-200') +
              (isEdit ? ' opacity-60 cursor-not-allowed' : '')
            }
            title={
              isEdit
                ? 'Transport cannot be changed after creation. Remove and re-add the server to switch.'
                : t === 'http'
                ? 'Streamable HTTP / SSE'
                : 'Local subprocess (npx, uvx, …)'
            }
          >
            {t === 'http' ? (
              <Globe className="h-3 w-3" />
            ) : (
              <Terminal className="h-3 w-3" />
            )}
            {t}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="text-xs text-zinc-400 space-y-1">
          <span>Name *</span>
          <input
            value={form.name}
            onChange={(e) => update('name', e.target.value)}
            disabled={isEdit}
            placeholder="mymcp"
            className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 focus:outline-none focus:border-violet-500 disabled:opacity-60"
          />
        </label>

        {transport === 'http' ? (
          <>
            <label className="text-xs text-zinc-400 space-y-1">
              <span>Server URL *</span>
              <input
                value={form.url || ''}
                onChange={(e) => update('url', e.target.value)}
                placeholder="https://example.com/mcp"
                className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
              />
            </label>
            <div className="text-xs text-zinc-400 space-y-1 sm:col-span-2">
              <span>Auth</span>
              <div className="flex">
                <div className="inline-flex rounded-md border border-zinc-800 bg-zinc-900 p-0.5 flex-wrap">
                  {(Object.keys(AUTH_LABELS) as AuthChoice[]).map((choice) => (
                    <button
                      key={choice}
                      type="button"
                      onClick={() => setAuthChoice(choice)}
                      className={
                        'rounded px-3 py-1 text-xs ' +
                        (authChoice === choice
                          ? 'bg-violet-600 text-white'
                          : 'text-zinc-400 hover:text-zinc-200')
                      }
                    >
                      {AUTH_LABELS[choice]}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            {authChoice === 'oauth' && (
              <>
                <div className="text-[11px] text-zinc-500 sm:col-span-2">
                  Flight Deck finds the server's authorization server and registers itself
                  there (Dynamic Client Registration), then you sign in in a popup. Fill in a
                  client ID only for a server without registration — register it with the
                  redirect URI <span className="font-mono text-zinc-400">{window.location.origin}/fd/mcp/oauth/callback</span>.
                </div>
                <label className="text-xs text-zinc-400 space-y-1 sm:col-span-2">
                  <span>Scopes</span>
                  <input
                    value={form.scope || ''}
                    onChange={(e) => update('scope', e.target.value)}
                    placeholder="optional — leave empty for the server's own"
                    className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
                  />
                </label>
              </>
            )}
            {authChoice !== 'none' && (
              <>
                <label className="text-xs text-zinc-400 space-y-1">
                  <span>Client ID</span>
                  <input
                    value={form.client_id || ''}
                    onChange={(e) => update('client_id', e.target.value)}
                    placeholder={
                      authChoice === 'oauth'
                        ? 'optional — empty registers automatically'
                        : 'OAuth2 client_credentials client'
                    }
                    className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
                  />
                </label>
                <label className="text-xs text-zinc-400 space-y-1">
                  <span>Client Secret</span>
                  <input
                    type="password"
                    value={form.client_secret || ''}
                    onChange={(e) => update('client_secret', e.target.value)}
                    placeholder={isEdit ? 'leave blank to keep existing' : 'optional'}
                    className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
                  />
                </label>
              </>
            )}
            {authChoice === 'client_credentials' && (
              <label className="text-xs text-zinc-400 space-y-1 sm:col-span-2">
                <span>Token endpoint</span>
                <input
                  value={form.token_endpoint || ''}
                  onChange={(e) => update('token_endpoint', e.target.value)}
                  placeholder="absolute URL or path, e.g. /api/mcp/oauth/token"
                  className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
                />
              </label>
            )}
          </>
        ) : (
          <>
            <label className="text-xs text-zinc-400 space-y-1">
              <span>Command *</span>
              <input
                value={form.command || ''}
                onChange={(e) => update('command', e.target.value)}
                placeholder="uvx"
                className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 font-mono focus:outline-none focus:border-violet-500"
              />
            </label>
            <label className="text-xs text-zinc-400 space-y-1 sm:col-span-2">
              <span>Args (space-separated)</span>
              <input
                value={argsText}
                onChange={(e) => setArgsText(e.target.value)}
                placeholder="mcp-server-filesystem /tmp"
                className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 font-mono focus:outline-none focus:border-violet-500"
              />
            </label>
            <label className="text-xs text-zinc-400 space-y-1 sm:col-span-2">
              <span>Environment (KEY=VALUE per line)</span>
              <textarea
                value={envText}
                onChange={(e) => setEnvText(e.target.value)}
                rows={3}
                placeholder={'PATH=/usr/local/bin:/usr/bin\nHOME=/tmp'}
                className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-2.5 py-1.5 text-sm text-zinc-100 font-mono focus:outline-none focus:border-violet-500"
              />
            </label>
          </>
        )}
      </div>

      <label className="flex items-center gap-2 text-xs text-zinc-400">
        <input
          type="checkbox"
          checked={form.enabled !== false}
          onChange={(e) => update('enabled', e.target.checked)}
        />
        <span>Enabled (agents will load tools from this server)</span>
      </label>

      {canShareDeckWide && (
        <label className="flex items-start gap-2 text-xs text-zinc-400">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={!!form.deck_wide}
            onChange={(e) => update('deck_wide', e.target.checked)}
          />
          <span>
            Share with everyone on this deck — every user's agents may use it (subject to the
            allowed agents below), and only admins can change it. Off: only your agents.
            {form.deck_wide && authChoice === 'oauth' && (
              <span className="block text-amber-400 mt-0.5">
                Every user's agents will act as the account signed in here.
              </span>
            )}
          </span>
        </label>
      )}

      <AgentAllowlistPicker
        value={form.allowed_agents || []}
        onChange={(next) => update('allowed_agents', next)}
        className="rounded-md border border-zinc-800 bg-zinc-900/40 px-3 py-2.5"
      />

      {probe && (
        <div
          className={
            'rounded-md border px-3 py-2 text-xs break-all ' +
            (probe.ok
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
              : 'border-red-500/30 bg-red-500/10 text-red-300')
          }
        >
          {probe.ok && !probe.needs_sign_in
            ? `Probe ok — discovered ${probe.tools_count ?? 0} tool${
                (probe.tools_count ?? 0) === 1 ? '' : 's'
              }${
                probe.tool_names && probe.tool_names.length
                  ? `: ${probe.tool_names.slice(0, 8).join(', ')}${
                      probe.tool_names.length > 8 ? '…' : ''
                    }`
                  : ''
              }`
            : probeText(probe)}
        </div>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onProbe}
          disabled={!valid || probing}
          className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 hover:bg-zinc-800 px-3 py-1.5 text-sm text-zinc-200 disabled:opacity-50"
        >
          {probing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
          Test connection
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={!valid || saving}
          className="inline-flex items-center gap-1.5 rounded-md bg-violet-600 hover:bg-violet-500 px-3 py-1.5 text-sm text-white disabled:opacity-50"
        >
          {saving ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : signInOnSave ? (
            <LogIn className="h-3.5 w-3.5" />
          ) : isEdit ? (
            <Check className="h-3.5 w-3.5" />
          ) : (
            <Plus className="h-3.5 w-3.5" />
          )}
          {signInOnSave ? 'Save & sign in' : isEdit ? 'Save changes' : 'Save server'}
        </button>
      </div>
    </div>
  )
}

export default function MCPConnection() {
  const { servers, loading, error, notice, refresh, startMessageListener } = useMCPStore()
  const canManageStdio = useCanManageStdio()
  const [collapsed, setCollapsed] = useState(false)
  const [adding, setAdding] = useState(false)
  const [editingName, setEditingName] = useState<string | null>(null)

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => startMessageListener(), [startMessageListener])

  const totalTools = servers.reduce(
    (sum, s) => sum + (s.tools_count || 0),
    0,
  )
  const connectedCount = servers.filter((s) => s.initialized).length

  const pill = 'inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border'

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 overflow-hidden">
      {/* Header */}
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className={
          'w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-zinc-900/80 transition-colors ' +
          (collapsed ? '' : 'border-b border-zinc-800')
        }
        aria-expanded={!collapsed}
      >
        <div className="h-10 w-10 rounded-md bg-gradient-to-br from-fuchsia-500/20 to-violet-500/20 border border-zinc-800 flex items-center justify-center">
          <Plug className="h-5 w-5 text-zinc-200" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-zinc-100">MCP servers</h3>
            {servers.length > 0 ? (
              <span className={`${pill} bg-emerald-500/15 text-emerald-500 border-emerald-500/30`}>
                <Check className="h-3 w-3" />
                {connectedCount}/{servers.length} connected
              </span>
            ) : (
              <span className={`${pill} bg-zinc-500/15 text-zinc-400 border-zinc-500/30`}>
                None configured
              </span>
            )}
            {totalTools > 0 && (
              <span className={`${pill} bg-blue-500/15 text-blue-500 border-blue-500/30`}>
                {totalTools} tool{totalTools === 1 ? '' : 's'}
              </span>
            )}
          </div>
          <div className="text-xs text-zinc-500 mt-0.5">
            Model Context Protocol — Flight Deck proxies tool calls (HTTP and stdio) to MCP servers and exposes them to your fleet, with per-agent allowlists.
          </div>
        </div>
        {loading && <Loader2 className="h-4 w-4 animate-spin text-zinc-500" />}
        <ChevronDown
          className={
            'h-4 w-4 text-zinc-500 transition-transform ' +
            (collapsed ? '-rotate-90' : '')
          }
        />
      </button>

      {/* Body */}
      {!collapsed && (
        <div className="px-5 py-4 space-y-3">
          {error && (
            <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-500">
              <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
              <span className="break-all">{error}</span>
            </div>
          )}
          {notice && !error && (
            <div className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400">
              <Check className="h-3.5 w-3.5 shrink-0 mt-0.5" />
              <span className="break-all">{notice}</span>
            </div>
          )}

          {servers.length === 0 && !adding && (
            <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-3 text-xs text-zinc-400">
              No MCP servers configured yet. Add one to expose its tools to every agent in the fleet.
            </div>
          )}

          <div className="space-y-2">
            {servers.map((srv) => {
              const readOnly =
                srv.can_manage === false || (srv.transport === 'stdio' && !canManageStdio)
              return editingName === srv.name && !readOnly ? (
                <ServerForm
                  key={srv.name}
                  initial={srv}
                  onClose={() => setEditingName(null)}
                  allowStdio={canManageStdio}
                />
              ) : (
                <ServerRow
                  key={srv.name}
                  server={srv}
                  readOnly={readOnly}
                  onEdit={() => {
                    setAdding(false)
                    setEditingName(srv.name)
                  }}
                />
              )
            })}
          </div>

          {adding ? (
            <ServerForm onClose={() => setAdding(false)} allowStdio={canManageStdio} />
          ) : (
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => {
                  setEditingName(null)
                  setAdding(true)
                }}
                className="inline-flex items-center gap-1.5 rounded-md bg-violet-600 hover:bg-violet-500 px-3.5 py-2 text-sm text-white"
              >
                <Plus className="h-4 w-4" />
                Add MCP server
              </button>
              <button
                type="button"
                onClick={refresh}
                disabled={loading}
                className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 hover:bg-zinc-800 px-3 py-2 text-sm text-zinc-200 disabled:opacity-50"
              >
                {loading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4" />
                )}
                Refresh
              </button>
            </div>
          )}
        </div>
      )}

    </div>
  )
}
