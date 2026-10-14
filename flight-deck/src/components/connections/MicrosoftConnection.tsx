import { useEffect, useState } from 'react'
import {
  Loader2,
  Check,
  Unplug,
  ExternalLink,
  Save,
  Eye,
  EyeOff,
  AlertCircle,
  KeyRound,
  Trash2,
  ChevronDown,
  LayoutGrid,
} from 'lucide-react'
import { useMicrosoftAuthStore } from '../../stores/microsoftAuthStore'
import { useAuthStore } from '../../stores/authStore'
import { AgenticSignatureField, normalizeSignature } from './AgenticSignatureField'

// The Microsoft twin of GoogleConnection: one app registration per deck
// (admin-managed — POST /fd/microsoft/config is require_admin), one Microsoft
// account per Flight Deck user. Agents read mail, draft (never send), organize
// mail when asked (archive, move, folders, categories), use the calendar and
// OneDrive with their owner's account.
export default function MicrosoftConnection() {
  const {
    status,
    config,
    loading,
    error,
    lastPopupMessage,
    refresh,
    saveConfig,
    clearCredentials,
    connect,
    disconnect,
    startMessageListener,
  } = useMicrosoftAuthStore()

  const authEnabled = useAuthStore((s) => s.authEnabled)
  const fdUser = useAuthStore((s) => s.user)
  const canManageClient = !authEnabled || fdUser?.role === 'admin'
  const fdUserLabel = authEnabled && fdUser ? (fdUser.display_name || fdUser.email) : ''
  const unavailable = authEnabled === false

  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [tenant, setTenant] = useState('')
  const [scopes, setScopes] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [revealSecret, setRevealSecret] = useState(false)
  const [collapsed, setCollapsed] = useState(false)

  useEffect(() => {
    if (unavailable) return
    refresh()
    const unsub = startMessageListener()
    return unsub
  }, [unavailable, refresh, startMessageListener])

  useEffect(() => {
    if (config) {
      setClientId(config.client_id || '')
      setTenant(config.tenant || 'common')
      setScopes(config.scopes || config.default_scopes || [])
    }
  }, [config])

  const configured = !!status?.configured
  const connected = !!status?.connected
  const user = status?.user
  const required = new Set(config?.required_scopes || [])

  const prevScopeSet = new Set(config?.scopes || [])
  const scopesDirty = prevScopeSet.size !== scopes.length || scopes.some((s) => !prevScopeSet.has(s))
  const tenantDirty = tenant.trim() !== (config?.tenant || 'common')
  const canSave =
    clientId.trim() !== (config?.client_id || '').trim() ||
    clientSecret.trim() !== '' ||
    tenantDirty ||
    scopesDirty

  const handleSave = async () => {
    const patch: Record<string, string | string[]> = {}
    if (clientId.trim() !== (config?.client_id || '')) patch.client_id = clientId.trim()
    if (clientSecret.trim()) patch.client_secret = clientSecret.trim()
    if (tenantDirty) patch.tenant = tenant.trim() || 'common'
    if (scopesDirty) patch.scopes = scopes
    if (Object.keys(patch).length === 0) return
    // A new client, tenant or scope set disconnects every user (their grants
    // belong to the old one).
    if (configured && (patch.client_id !== undefined || patch.tenant !== undefined || scopesDirty) && !confirm(
      'Change the Microsoft app registration / tenant / scopes?\n\n' +
      'This disconnects the Microsoft account of EVERY user on this deck, not ' +
      'just yours. Their agents lose Microsoft access until each user connects again.',
    )) return
    setSaving(true)
    await saveConfig(patch)
    setClientSecret('')
    setSaving(false)
  }

  const toggleScope = (scope: string) => {
    setScopes((prev) => (prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope]))
  }

  const handleClear = async () => {
    if (!confirm(
      "Remove this deck's Microsoft app registration?\n\n" +
      'This clears the stored client ID and secret and disconnects the Microsoft ' +
      'account of EVERY user on this deck.',
    )) return
    setSaving(true)
    await clearCredentials()
    setClientId('')
    setClientSecret('')
    setSaving(false)
  }

  const pill = 'inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border'
  const pillTiny = 'inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border'
  const icon = (
    <div className="h-10 w-10 rounded-md bg-gradient-to-br from-sky-500/20 to-emerald-500/20 border border-zinc-800 flex items-center justify-center">
      <LayoutGrid className="h-5 w-5 text-zinc-200" />
    </div>
  )

  if (unavailable) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 overflow-hidden">
        <div className="flex items-center gap-3 px-5 py-4 border-b border-zinc-800">
          {icon}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-sm font-semibold text-zinc-100">Microsoft</h3>
              <span className={`${pill} bg-zinc-800 text-zinc-400 border-zinc-700`}>Not available</span>
            </div>
            <div className="text-xs text-zinc-500 mt-0.5">Outlook mail, Calendar, OneDrive</div>
          </div>
        </div>
        <div className="px-5 py-4 text-xs text-zinc-400 leading-relaxed">
          Microsoft isn't available on this deck: Flight Deck sign-in is turned off, and each
          Microsoft connection belongs to a signed-in Flight Deck user. Run Flight Deck with
          sign-in on (<code className="text-zinc-300">FD_AUTH_ENABLED=true</code>, the default)
          to connect Microsoft.
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 overflow-hidden">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className={
          'w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-zinc-900/80 transition-colors ' +
          (collapsed ? '' : 'border-b border-zinc-800')
        }
        aria-expanded={!collapsed}
      >
        {icon}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-zinc-100">Microsoft</h3>
            {connected ? (
              <span className={`${pill} bg-emerald-500/15 text-emerald-500 border-emerald-500/30`}>
                <Check className="h-3 w-3" /> Connected
              </span>
            ) : configured ? (
              <span className={`${pill} bg-amber-500/15 text-amber-600 border-amber-500/30`}>Not connected</span>
            ) : (
              <span className={`${pill} bg-red-500/15 text-red-500 border-red-500/30`}>
                <AlertCircle className="h-3 w-3" /> Not configured
              </span>
            )}
            <span
              className={`${pillTiny} bg-blue-500/15 text-blue-500 border-blue-500/30`}
              title="This deck uses its own Microsoft Entra app registration — one for all users, managed by an admin. Each Flight Deck user connects their own Microsoft account through it."
            >
              <KeyRound className="h-2.5 w-2.5" />
              own app registration
            </span>
          </div>
          <div className="text-xs text-zinc-500 mt-0.5">Outlook mail (read, drafts, organize), Calendar, OneDrive</div>
        </div>
        {loading && <Loader2 className="h-4 w-4 animate-spin text-zinc-500" />}
        <ChevronDown className={'h-4 w-4 text-zinc-500 transition-transform ' + (collapsed ? '-rotate-90' : '')} />
      </button>

      {!collapsed && (
      <div className="px-5 py-4 space-y-4">
        {error && (
          <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-500">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            <span className="break-all">{error}</span>
          </div>
        )}

        {lastPopupMessage && (
          <div className="rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2 text-xs text-zinc-400">
            {lastPopupMessage}
          </div>
        )}

        {!configured && !canManageClient && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-xs text-zinc-300">
            <div className="font-medium text-amber-600 mb-0.5">Microsoft isn't set up on this deck yet</div>
            <div className="text-zinc-400">
              Ask an admin to register the Microsoft app. Once they have, you can connect your own
              Microsoft account here.
            </div>
          </div>
        )}
        {!configured && canManageClient && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-xs text-zinc-300">
            <div className="font-medium text-amber-600 mb-0.5">Register an app in Microsoft Entra</div>
            <div className="text-zinc-400 space-y-1">
              <div>
                In the{' '}
                <a
                  href="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-violet-500 hover:underline"
                >
                  Entra admin center → App registrations
                </a>
                , create a <span className="text-zinc-200 font-medium">New registration</span>. Supported
                account types: <span className="text-zinc-200">any organizational directory and personal
                Microsoft accounts</span> (tenant <code>common</code>), or only your organization (put its
                tenant ID below).
              </div>
              <div>
                Add a <span className="text-zinc-200 font-medium">Web</span> redirect URI — the one shown below
                — then under Certificates &amp; secrets create a client secret and paste the Application
                (client) ID and the secret's <span className="text-zinc-200">Value</span> here. Graph
                permissions are requested at sign-in; no admin consent is needed unless your organization
                requires it.
              </div>
            </div>
          </div>
        )}

        {connected && user && (
          <div
            className="flex items-center gap-3 rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2.5"
            title={fdUserLabel ? `Microsoft account linked to ${fdUserLabel}` : undefined}
          >
            <div className="flex-1 min-w-0">
              <div className="text-sm text-zinc-200 truncate">{user.name || user.email}</div>
              {user.email && user.name && <div className="text-xs text-zinc-500 truncate">{user.email}</div>}
            </div>
          </div>
        )}

        {connected && status?.granted_scopes && status.granted_scopes.length > 0 && (
          <div>
            <div className="text-xs text-zinc-500 mb-1.5">Granted access</div>
            <div className="flex flex-wrap gap-1.5">
              {status.granted_scopes.map((s) => (
                <span
                  key={s.scope}
                  className="text-xs px-2 py-0.5 rounded-full bg-zinc-800 text-zinc-300 border border-zinc-700"
                  title={s.scope}
                >
                  {s.label}
                </span>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 flex-wrap">
          {!connected && (
            <button
              onClick={connect}
              disabled={!configured}
              className="inline-flex items-center gap-2 rounded-md bg-violet-600 hover:bg-violet-500 px-3.5 py-2 text-sm text-white shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
              title={
                configured
                  ? 'Link a Microsoft account to your own Flight Deck user'
                  : canManageClient ? 'Save the app registration first' : 'An admin has to configure Microsoft first'
              }
            >
              <ExternalLink className="h-4 w-4" />
              Connect Microsoft
            </button>
          )}
          {connected && (
            <button
              onClick={disconnect}
              disabled={loading}
              className="inline-flex items-center gap-2 rounded-md border border-zinc-700 hover:bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 disabled:opacity-50"
            >
              <Unplug className="h-3.5 w-3.5" />
              Disconnect
            </button>
          )}
          {configured && fdUserLabel && (
            <span className="text-xs text-zinc-500">
              {connected ? 'Linked to' : 'Links a Microsoft account to'} your Flight Deck user,{' '}
              <span className="text-zinc-300">{fdUserLabel}</span>. Your agents use it — every other
              user connects their own.
            </span>
          )}
        </div>

        {connected && (
          <div className="text-[11px] text-zinc-500 leading-snug">
            Agents never send Outlook mail: they save drafts in your Outlook Drafts folder for you to
            send. They archive, move or categorize mail, or create folders, only when you ask.
            Calendar changes that email attendees (invitations, updates, cancellations) happen only
            when you ask for them.
          </div>
        )}

        {/* The signed-in user's own setting, so it shows for non-admins and
            in the kiosk Connections dialog too. */}
        {connected && <OutlookSignatureSection />}

        {canManageClient && (
        <div className="space-y-3 rounded-md border border-zinc-800 bg-zinc-950/50 p-3">
          <div className="text-xs text-zinc-500">
            Redirect URI (add it as a <span className="text-zinc-300">Web</span> redirect URI on the app registration):
            <code className="block mt-1 text-[11px] text-zinc-200 bg-zinc-900 border border-zinc-800 rounded px-2 py-1 break-all">
              {config?.redirect_uri || `${window.location.origin}/fd/microsoft/callback`}
            </code>
            <div className="mt-1 text-[11px] text-zinc-600">
              Microsoft accepts plain http only for localhost — a deck reached by name needs https
              (set FD_PUBLIC_URL behind a proxy).
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-zinc-400 mb-1">
              Application (client) ID
              {config?.client_id_set && <span className="ml-2 text-[10px] text-emerald-500">(saved)</span>}
            </label>
            <input
              type="text"
              value={clientId}
              onChange={(e) => setClientId(e.target.value)}
              placeholder="00000000-0000-0000-0000-000000000000"
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 font-mono focus:border-violet-500 focus:outline-none"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-zinc-400 mb-1">
              Client secret (Value)
              {config?.client_secret_set && !clientSecret && (
                <span className="ml-2 text-[10px] text-emerald-500">(saved)</span>
              )}
            </label>
            <div className="relative">
              <input
                type={revealSecret ? 'text' : 'password'}
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                placeholder={config?.client_secret_set ? '•••••••• (leave blank to keep)' : 'secret value'}
                className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 pr-9 text-sm text-zinc-200 font-mono focus:border-violet-500 focus:outline-none"
              />
              <button
                type="button"
                onClick={() => setRevealSecret((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
                tabIndex={-1}
              >
                {revealSecret ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </button>
            </div>
            <div className="mt-1 text-[11px] text-zinc-600">
              Client secrets expire (at most 24 months) — when this one does, every connection stops
              refreshing until you paste a new one here.
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-zinc-400 mb-1">Tenant</label>
            <input
              type="text"
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              placeholder="common"
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 font-mono focus:border-violet-500 focus:outline-none"
            />
            <div className="mt-1 text-[11px] text-zinc-600">
              <code>common</code> = work/school and personal accounts · <code>organizations</code> = any
              work/school · <code>consumers</code> = personal only · or your directory (tenant) ID /
              domain for a single-tenant app.
            </div>
          </div>

          {config?.scope_catalog && config.scope_catalog.length > 0 && (
            <div>
              <div className="text-xs font-medium text-zinc-400 mb-1.5">Requested permissions (Microsoft Graph, delegated)</div>
              <div className="rounded-md border border-zinc-800 bg-zinc-950/60 divide-y divide-zinc-800">
                {config.scope_catalog.map((entry) => {
                  const disabled = required.has(entry.scope)
                  const checked = disabled || scopes.includes(entry.scope)
                  return (
                    <label
                      key={entry.scope}
                      className={
                        'flex items-start gap-2 px-3 py-2 cursor-pointer hover:bg-zinc-900/50 ' +
                        (disabled ? 'opacity-70 cursor-not-allowed' : '')
                      }
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={disabled}
                        onChange={() => !disabled && toggleScope(entry.scope)}
                        className="mt-0.5 accent-violet-600"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="text-sm text-zinc-200">{entry.label}</span>
                          <code className="text-[10px] text-zinc-500">{entry.scope}</code>
                        </div>
                        <div className="text-xs text-zinc-500 mt-0.5">{entry.description}</div>
                      </div>
                    </label>
                  )
                })}
              </div>
              <div className="mt-1.5 text-[11px] text-zinc-500 leading-snug">
                Users consent to these themselves; an organization can require admin consent instead
                (then an admin grants it once in Entra → Enterprise applications). Changing the
                permissions disconnects every user's Microsoft account; each has to reconnect.
              </div>
            </div>
          )}

          <div className="flex items-center gap-2 pt-1 flex-wrap">
            <button
              onClick={handleSave}
              disabled={saving || !canSave}
              className="inline-flex items-center gap-2 rounded-md bg-violet-600 hover:bg-violet-500 px-3 py-1.5 text-sm text-white disabled:opacity-50 shadow-sm"
            >
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              Save app registration
            </button>
            {configured && (
              <button
                onClick={handleClear}
                disabled={saving}
                className="inline-flex items-center gap-2 rounded-md border border-red-500/30 hover:bg-red-500/10 px-3 py-1.5 text-xs text-red-500 disabled:opacity-50"
              >
                <Trash2 className="h-3 w-3" />
                Remove app registration
              </button>
            )}
            {configured && !connected && (
              <span className="text-xs text-zinc-500">
                Then click <span className="text-zinc-300 font-medium">Connect Microsoft</span> above.
              </span>
            )}
          </div>
        </div>
        )}
      </div>
      )}
    </div>
  )
}

// The user's agentic signature for Outlook drafts (GET/PUT
// /fd/microsoft/mail-signature) — the Google twin lives in the Email sending
// section, under the recipients list.
function OutlookSignatureSection() {
  const saved = useMicrosoftAuthStore((s) => s.signature)
  const error = useMicrosoftAuthStore((s) => s.signatureError)
  const fetchSignature = useMicrosoftAuthStore((s) => s.fetchSignature)
  const saveSignature = useMicrosoftAuthStore((s) => s.saveSignature)

  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    fetchSignature()
  }, [fetchSignature])

  // Re-seed the box when the saved value changes (load, save) — done while
  // rendering, so it never paints a frame empty.
  const [seeded, setSeeded] = useState<string | null>(null)
  if (saved !== null && saved !== seeded) {
    setSeeded(saved)
    setText(saved)
  }

  const signature = normalizeSignature(text)
  const dirty = saved !== null && signature !== saved

  const handleSave = async () => {
    if (!dirty || saving) return
    setSaving(true)
    await saveSignature(signature)
    setSaving(false)
  }

  return (
    <div className="space-y-3 rounded-md border border-zinc-800 bg-zinc-950/50 p-3">
      {error && (
        <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-500">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span className="break-words">{error}</span>
        </div>
      )}
      {saved === null ? (
        !error && (
          <div className="flex items-center gap-2 text-xs text-zinc-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
          </div>
        )
      ) : (
        <>
          <AgenticSignatureField value={text} onChange={setText} disabled={saving} writes="draft" />
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={handleSave}
              disabled={!dirty || saving}
              className="inline-flex items-center gap-2 rounded-md bg-violet-600 hover:bg-violet-500 px-3 py-1.5 text-sm text-white disabled:opacity-50 shadow-sm"
            >
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              Save
            </button>
            {dirty && !saving && <span className="text-xs text-zinc-500">Unsaved changes</span>}
          </div>
        </>
      )}
    </div>
  )
}
