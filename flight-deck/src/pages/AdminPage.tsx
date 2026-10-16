import { useEffect, useState, useCallback } from 'react'
import {
  Shield,
  Users,
  UserPlus,
  BarChart3,
  RefreshCw,
  Trash2,
  ChevronDown,
  ChevronRight,
  Crown,
  Loader2,
  Settings2,
  Key,
  Save,
  Check,
  Bot,
  Plug,
  MessageSquare,
} from 'lucide-react'
import { useAuthStore } from '../stores/authStore'
import { UserAgentsDialog } from '../components/agents/UserAgentsDialog'
import { DeckProfileDefaults } from '../components/profile/DeckProfileDefaults'
import { SharedTelegramBotAdmin } from '../components/admin/SharedTelegramBotAdmin'
import { SharedCredentialsAdmin } from '../components/admin/SharedCredentialsAdmin'
import { ADMIN_TAB_KEY } from '../services/sharedIntegrations'

type AdminTab = 'users' | 'usage' | 'plans' | 'settings' | 'integrations'

// Other pages may open Admin on a given tab (Connections → Telegram sends an
// admin to Integrations, see ADMIN_TAB_KEY); otherwise it opens on Users.
function takeRequestedTab(): AdminTab {
  try {
    const t = sessionStorage.getItem(ADMIN_TAB_KEY)
    sessionStorage.removeItem(ADMIN_TAB_KEY)
    if (t === 'usage' || t === 'plans' || t === 'settings' || t === 'integrations') return t
  } catch { /* storage blocked */ }
  return 'users'
}

// ── Types ──

interface UserInfo {
  id: string
  email: string
  display_name: string
  role: string
  created_at: string
  updated_at: string
  metadata: string
}

// Kiosk (simple-chat) lock for the deck: nobody, every non-admin, or the users
// marked in the Users tab. Admins are never locked.
type KioskMode = 'off' | 'all' | 'selected'

interface SystemConfig {
  docker_spawn_enabled: boolean
  kiosk_mode: KioskMode
  kiosk_new_users: boolean
  // The mode is the `--simple-chat` default — no admin has picked one yet.
  kiosk_mode_from_env?: boolean
}

const CONFIG_DEFAULTS: SystemConfig = {
  docker_spawn_enabled: true, kiosk_mode: 'off', kiosk_new_users: false,
}

const KIOSK_MODES: { value: KioskMode; label: string; hint: string }[] = [
  { value: 'off', label: 'Off', hint: 'Everyone gets the normal layout.' },
  { value: 'all', label: 'Everyone', hint: 'Every non-admin is locked to simple chat.' },
  { value: 'selected', label: 'Selected users', hint: 'Only the users marked in the Users tab are locked.' },
]

function userKioskFlag(user: UserInfo): boolean {
  try { return JSON.parse(user.metadata || '{}').kiosk === true } catch { return false }
}

// What the server resolves as the user's lock (auth_routes._user_payload).
function kioskEffective(role: string, flag: boolean, mode: KioskMode): boolean {
  if (role === 'admin') return false
  return mode === 'all' || (mode === 'selected' && flag)
}

function kioskModeNote(mode: KioskMode): string | null {
  if (mode === 'all') return 'Kiosk mode is on for everyone (Settings), so this user is locked either way.'
  if (mode === 'off') return 'Kiosk mode is off (Settings); this takes effect under "Selected users".'
  return null
}

interface UsageLog {
  id: number
  user_id: string
  event_type: string
  detail: string
  created_at: string
}

interface PlanLimits {
  max_agents: number
  max_storage_mb: number
  requests_per_minute: number
  spawns_per_hour: number
}

interface CostUserRow {
  user_id: string
  email: string | null
  display_name: string | null
  runs: number
  usd: number
  priced: number
}
interface CostKindRow {
  run_kind: string
  runs: number
  usd: number
}
interface CostRollup {
  by_user: CostUserRow[]
  by_kind: CostKindRow[]
  total_usd: number
}

// ── API helpers ──

function _headers(): Record<string, string> {
  const { token } = useAuthStore.getState()
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

async function fetchUsers(limit = 100, offset = 0): Promise<{ users: UserInfo[]; total: number }> {
  const res = await fetch(`/fd/admin/users?limit=${limit}&offset=${offset}`, {
    headers: _headers(), credentials: 'include',
  })
  if (!res.ok) throw new Error('Failed to fetch users')
  return res.json()
}

async function createUser(payload: {
  email: string
  password: string
  display_name?: string
  role?: string
  kiosk?: boolean
}): Promise<void> {
  const res = await fetch('/fd/admin/users', {
    method: 'POST', headers: _headers(), credentials: 'include',
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data.detail || 'Failed to create user')
  }
}

async function updateUser(userId: string, patch: Record<string, unknown>): Promise<void> {
  const res = await fetch(`/fd/admin/users/${userId}`, {
    method: 'PUT', headers: _headers(), credentials: 'include',
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error('Failed to update user')
}

async function deleteUser(userId: string): Promise<void> {
  const res = await fetch(`/fd/admin/users/${userId}`, {
    method: 'DELETE', headers: _headers(), credentials: 'include',
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data.detail || 'Failed to delete user')
  }
}

async function fetchUsageSummary(userId?: string, since?: string): Promise<Record<string, number>> {
  const params = new URLSearchParams()
  if (userId) params.set('user_id', userId)
  if (since) params.set('since', since)
  const res = await fetch(`/fd/admin/usage/summary?${params}`, {
    headers: _headers(), credentials: 'include',
  })
  if (!res.ok) throw new Error('Failed to fetch usage')
  const data = await res.json()
  return data.summary
}

async function fetchUsageLogs(userId?: string, eventType?: string, limit = 50): Promise<UsageLog[]> {
  const params = new URLSearchParams()
  if (userId) params.set('user_id', userId)
  if (eventType) params.set('event_type', eventType)
  params.set('limit', String(limit))
  const res = await fetch(`/fd/admin/usage?${params}`, {
    headers: _headers(), credentials: 'include',
  })
  if (!res.ok) throw new Error('Failed to fetch usage logs')
  const data = await res.json()
  return data.logs
}

async function fetchCostRollup(since?: string, until?: string): Promise<CostRollup> {
  const params = new URLSearchParams()
  if (since) params.set('since', since)
  if (until) params.set('until', until)
  const qs = params.toString() ? `?${params}` : ''
  const res = await fetch(`/fd/admin/costs${qs}`, { headers: _headers(), credentials: 'include' })
  if (!res.ok) throw new Error('Failed to fetch cost rollup')
  return res.json()
}

async function fetchConfig(): Promise<SystemConfig> {
  const res = await fetch('/fd/admin/config', {
    headers: _headers(), credentials: 'include',
  })
  if (!res.ok) return { ...CONFIG_DEFAULTS }
  return { ...CONFIG_DEFAULTS, ...(await res.json()) }
}

async function saveConfig(cfg: Partial<SystemConfig>): Promise<SystemConfig> {
  const res = await fetch('/fd/admin/config', {
    method: 'PUT', headers: _headers(), credentials: 'include',
    body: JSON.stringify(cfg),
  })
  if (!res.ok) throw new Error('Failed to save config')
  return { ...CONFIG_DEFAULTS, ...(await res.json()) }
}

async function fetchProviderKeys(): Promise<Record<string, string>> {
  const res = await fetch('/fd/admin/provider-keys', {
    headers: _headers(), credentials: 'include',
  })
  if (!res.ok) return {}
  const data = await res.json()
  return data.keys || {}
}

async function saveProviderKeysApi(keys: Record<string, string>): Promise<Record<string, string>> {
  const res = await fetch('/fd/admin/provider-keys', {
    method: 'PUT', headers: _headers(), credentials: 'include',
    body: JSON.stringify({ keys }),
  })
  if (!res.ok) throw new Error('Failed to save provider keys')
  const data = await res.json()
  return data.keys || {}
}

const LLM_PROVIDERS = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'gemini', label: 'Gemini' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'xai', label: 'xAI' },
  { value: 'litert', label: 'LiteRT (local Gemma)' },
] as const

async function fetchPlans(): Promise<Record<string, PlanLimits>> {
  const res = await fetch('/fd/admin/plans', {
    headers: _headers(), credentials: 'include',
  })
  if (!res.ok) throw new Error('Failed to fetch plans')
  const data = await res.json()
  return data.plans
}

async function updatePlan(plan: string, limits: Partial<PlanLimits>): Promise<void> {
  const res = await fetch(`/fd/admin/plans/${plan}`, {
    method: 'PUT', headers: _headers(), credentials: 'include',
    body: JSON.stringify(limits),
  })
  if (!res.ok) throw new Error('Failed to update plan')
}

// ── Components ──

function CreateUserForm({ onCreated, config }: { onCreated: () => void; config: SystemConfig }) {
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [role, setRole] = useState('user')
  // Starts at the deck's "new users start in kiosk mode".
  const [kiosk, setKiosk] = useState(config.kiosk_new_users)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const reset = () => {
    setEmail(''); setPassword(''); setDisplayName(''); setRole('user')
    setKiosk(config.kiosk_new_users); setErr('')
  }
  useEffect(() => { setKiosk(config.kiosk_new_users) }, [config.kiosk_new_users])

  const valid = email.trim().includes('@') && password.length >= 6

  const handleCreate = async () => {
    if (!valid) return
    setSaving(true); setErr('')
    try {
      await createUser({
        email: email.trim(),
        password,
        display_name: displayName.trim() || undefined,
        role,
        ...(role === 'admin' ? {} : { kiosk }),
      })
      reset()
      setOpen(false)
      onCreated()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Failed to create user')
    }
    setSaving(false)
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-zinc-700 px-3 py-2.5 text-sm text-zinc-400 hover:border-violet-500/50 hover:text-zinc-200 transition-colors"
      >
        <UserPlus className="h-4 w-4" />
        Add user
      </button>
    )
  }

  return (
    <div className="rounded-lg border border-violet-800/40 bg-zinc-900/50 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <UserPlus className="h-4 w-4 text-violet-400" />
        <h3 className="text-sm font-semibold text-zinc-200">New user</h3>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="col-span-2">
          <label className="block text-xs font-medium text-zinc-500 mb-1">Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="teammate@example.com"
            autoComplete="off"
            className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-zinc-500 mb-1">Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="At least 6 characters"
            autoComplete="new-password"
            className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
          />
          {password && password.length < 6 && (
            <p className="mt-1 text-[11px] text-amber-500/80">At least 6 characters.</p>
          )}
        </div>
        <div>
          <label className="block text-xs font-medium text-zinc-500 mb-1">Role</label>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
          >
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
        </div>
        <div className="col-span-2">
          <label className="block text-xs font-medium text-zinc-500 mb-1">
            Display name <span className="text-zinc-600">(optional)</span>
          </label>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="Defaults to the email name"
            className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
          />
        </div>
        {role !== 'admin' && (
          <div className="col-span-2">
            <KioskCheckbox checked={kiosk} onChange={setKiosk} note={kioskModeNote(config.kiosk_mode)} />
          </div>
        )}
      </div>
      {err && <p className="text-xs text-red-400">{err}</p>}
      <div className="flex items-center gap-2 pt-1">
        <button
          onClick={handleCreate}
          disabled={saving || !valid}
          className="rounded-md bg-violet-600 px-3 py-1.5 text-xs text-white hover:bg-violet-500 disabled:opacity-50"
        >
          {saving ? 'Creating…' : 'Create user'}
        </button>
        <button
          onClick={() => { reset(); setOpen(false) }}
          className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

function KioskCheckbox({ checked, onChange, note }: {
  checked: boolean
  onChange: (v: boolean) => void
  note: string | null
}) {
  return (
    <div>
      <label className="flex items-center gap-2 text-sm text-zinc-300 cursor-pointer">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="h-3.5 w-3.5 rounded border-zinc-600 bg-zinc-950 accent-violet-600"
        />
        Kiosk mode <span className="text-xs text-zinc-500">— simple chat only</span>
      </label>
      {note && <p className="mt-1 ml-5 text-[11px] text-zinc-500">{note}</p>}
    </div>
  )
}

function Switch({ on, onToggle, disabled = false, label }: {
  on: boolean
  onToggle: () => void
  disabled?: boolean
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onToggle}
      disabled={disabled}
      className={`relative inline-flex h-6 w-11 shrink-0 rounded-full border-2 border-transparent transition-colors disabled:opacity-50 ${
        on ? 'bg-violet-600' : 'bg-zinc-700'
      }`}
    >
      <span className={`inline-block h-5 w-5 rounded-full bg-white transition-transform ${
        on ? 'translate-x-5' : 'translate-x-0'
      }`} />
    </button>
  )
}

function UserRow({
  user, plans, onUpdate, onDelete, currentUserId, kioskMode,
}: {
  user: UserInfo
  plans: Record<string, PlanLimits>
  onUpdate: () => void
  onDelete: (id: string) => void
  currentUserId: string
  kioskMode: KioskMode
}) {
  const [expanded, setExpanded] = useState(false)
  const [plan, setPlan] = useState(() => {
    try { return JSON.parse(user.metadata || '{}').plan || 'free' } catch { return 'free' }
  })
  const [role, setRole] = useState(user.role)
  const [kiosk, setKiosk] = useState(() => userKioskFlag(user))
  const [password, setPassword] = useState('')
  const [saving, setSaving] = useState(false)
  const [agentsOpen, setAgentsOpen] = useState(false)
  const isSelf = user.id === currentUserId

  const handleSave = async () => {
    setSaving(true)
    try {
      const patch: Record<string, unknown> = { plan, role, kiosk }
      if (password.trim()) patch.password = password
      await updateUser(user.id, patch)
      setPassword('')
      onUpdate()
    } catch { /* ignore */ }
    setSaving(false)
  }

  return (
    <div className="border border-zinc-800 rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 px-4 py-3 hover:bg-zinc-800/50 transition-colors text-left"
      >
        {expanded ? <ChevronDown className="h-4 w-4 text-zinc-500 shrink-0" /> : <ChevronRight className="h-4 w-4 text-zinc-500 shrink-0" />}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-zinc-200 truncate">{user.display_name || user.email}</span>
            {user.role === 'admin' && <Crown className="h-3.5 w-3.5 text-amber-400" />}
            {isSelf && <span className="text-xs text-zinc-500">(you)</span>}
          </div>
          <div className="text-xs text-zinc-500">{user.email}</div>
        </div>
        {kioskEffective(user.role, userKioskFlag(user), kioskMode) && (
          <span
            title="Locked to the simple chat layout (kiosk mode)"
            className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-violet-500/15 text-violet-300"
          >
            <MessageSquare className="h-3 w-3" />
            kiosk
          </span>
        )}
        <span className="text-xs px-2 py-0.5 rounded-full bg-zinc-800 text-zinc-400 capitalize">{plan}</span>
      </button>

      {expanded && (
        <div className="border-t border-zinc-800 px-4 py-3 bg-zinc-900/50 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-zinc-500 mb-1">Plan</label>
              <select
                value={plan}
                onChange={(e) => setPlan(e.target.value)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
              >
                {Object.keys(plans).map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-zinc-500 mb-1">Role</label>
              <select
                value={role}
                onChange={(e) => setRole(e.target.value)}
                disabled={isSelf}
                className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none disabled:opacity-50"
              >
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
            </div>
          </div>

          {role === 'admin' ? (
            <p className="text-[11px] text-zinc-500">Admins are never held to kiosk mode.</p>
          ) : (
            <KioskCheckbox checked={kiosk} onChange={setKiosk} note={kioskModeNote(kioskMode)} />
          )}

          <div>
            <label className="block text-xs font-medium text-zinc-500 mb-1">Reset password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="New password (leave blank to keep)"
              autoComplete="new-password"
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
            />
            {password && password.length < 6 && (
              <p className="mt-1 text-[11px] text-amber-500/80">At least 6 characters.</p>
            )}
          </div>

          {plans[plan] && (
            <div className="text-xs text-zinc-500 grid grid-cols-2 gap-1">
              <span>Max agents: {plans[plan].max_agents}</span>
              <span>Storage: {plans[plan].max_storage_mb} MB</span>
              <span>Requests/min: {plans[plan].requests_per_minute}</span>
              <span>Spawns/hour: {plans[plan].spawns_per_hour}</span>
            </div>
          )}

          <div className="text-xs text-zinc-600">
            Joined: {new Date(user.created_at).toLocaleDateString()}
          </div>

          <div className="flex items-center gap-2 pt-1">
            <button
              onClick={handleSave}
              disabled={saving}
              className="rounded-md bg-violet-600 px-3 py-1.5 text-xs text-white hover:bg-violet-500 disabled:opacity-50"
            >
              {saving ? 'Saving...' : 'Save Changes'}
            </button>
            <button
              onClick={() => setAgentsOpen(true)}
              title="Create, start, stop, configure and remove this user's agents"
              className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:border-violet-500/40 hover:text-zinc-100"
            >
              <Bot className="h-3 w-3 inline mr-1" />
              Agents
            </button>
            {!isSelf && (
              <button
                onClick={() => { if (confirm(`Delete user ${user.email}? This cannot be undone.`)) onDelete(user.id) }}
                className="rounded-md border border-red-900/50 px-3 py-1.5 text-xs text-red-400 hover:bg-red-950/50"
              >
                <Trash2 className="h-3 w-3 inline mr-1" />
                Delete
              </button>
            )}
          </div>
        </div>
      )}
      {agentsOpen && (
        <UserAgentsDialog
          userId={user.id}
          userLabel={user.display_name || user.email}
          onClose={() => setAgentsOpen(false)}
        />
      )}
    </div>
  )
}

const PLAN_FIELD_LABELS: Record<string, string> = {
  max_agents: 'Max Agents',
  max_storage_mb: 'Storage (MB)',
  requests_per_minute: 'Requests / min',
  spawns_per_hour: 'Spawns / hour',
}

function PlanCard({
  name, limits, onSave,
}: {
  name: string
  limits: PlanLimits
  onSave: (plan: string, limits: Partial<PlanLimits>) => Promise<void>
}) {
  const [draft, setDraft] = useState({ ...limits })
  const [saving, setSaving] = useState(false)
  const changed = Object.keys(PLAN_FIELD_LABELS).some(
    (k) => draft[k as keyof PlanLimits] !== limits[k as keyof PlanLimits]
  )

  const handleSave = async () => {
    setSaving(true)
    try { await onSave(name, draft) } catch { /* ignore */ }
    setSaving(false)
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-3">
      <h3 className="text-sm font-semibold text-zinc-200 capitalize">{name}</h3>
      <div className="grid grid-cols-2 gap-3">
        {Object.entries(PLAN_FIELD_LABELS).map(([key, label]) => (
          <div key={key}>
            <label className="block text-xs text-zinc-500 mb-1">{label}</label>
            <input
              type="number"
              min={0}
              value={draft[key as keyof PlanLimits]}
              onChange={(e) => setDraft({ ...draft, [key]: parseInt(e.target.value) || 0 })}
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
            />
          </div>
        ))}
      </div>
      {changed && (
        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded-md bg-violet-600 px-3 py-1.5 text-xs text-white hover:bg-violet-500 disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      )}
    </div>
  )
}

// ── Main Page ──

export function AdminPage() {
  const currentUser = useAuthStore((s) => s.user)
  const [tab, setTab] = useState<AdminTab>(takeRequestedTab)
  const [users, setUsers] = useState<UserInfo[]>([])
  const [totalUsers, setTotalUsers] = useState(0)
  const [plans, setPlans] = useState<Record<string, PlanLimits>>({})
  const [config, setConfig] = useState<SystemConfig>({ ...CONFIG_DEFAULTS })
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({})
  const [providerKeysDraft, setProviderKeysDraft] = useState<Record<string, string>>({})
  const [keySaveFlash, setKeySaveFlash] = useState<string | null>(null)
  const [usageSummary, setUsageSummary] = useState<Record<string, number>>({})
  const [usageLogs, setUsageLogs] = useState<UsageLog[]>([])
  const [costRollup, setCostRollup] = useState<CostRollup | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // Bumped by Refresh: the Integrations cards load on their own and reload on it.
  const [refreshTick, setRefreshTick] = useState(0)

  const loadUsers = useCallback(async () => {
    try {
      const [usersData, plansData, cfgData, keys] = await Promise.all([fetchUsers(), fetchPlans(), fetchConfig(), fetchProviderKeys()])
      setUsers(usersData.users)
      setTotalUsers(usersData.total)
      setPlans(plansData)
      setConfig(cfgData)
      setProviderKeys(keys)
      setProviderKeysDraft(keys)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load users')
    }
  }, [])

  const loadUsage = useCallback(async () => {
    try {
      const [summary, logs, costs] = await Promise.all([
        fetchUsageSummary(), fetchUsageLogs(), fetchCostRollup().catch(() => null),
      ])
      setUsageSummary(summary)
      setUsageLogs(logs)
      setCostRollup(costs)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load usage')
    }
  }, [])

  useEffect(() => {
    setLoading(true)
    Promise.all([loadUsers(), loadUsage()]).finally(() => setLoading(false))
  }, [loadUsers, loadUsage])

  const handleDelete = async (userId: string) => {
    try {
      await deleteUser(userId)
      loadUsers()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to delete user')
    }
  }

  const refresh = () => {
    setRefreshTick((t) => t + 1)
    setLoading(true)
    Promise.all([loadUsers(), loadUsage()]).finally(() => setLoading(false))
  }

  if (!currentUser || currentUser.role !== 'admin') {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-zinc-500 text-sm">Admin access required</div>
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-4xl mx-auto p-6 space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Shield className="h-5 w-5 text-violet-400" />
            <h1 className="text-lg font-semibold text-zinc-100">Admin Panel</h1>
          </div>
          <button
            onClick={refresh}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        {error && (
          <div className="rounded-md border border-red-900/50 bg-red-950/20 px-4 py-2 text-sm text-red-400">
            {error}
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b border-zinc-800">
          <button
            onClick={() => setTab('users')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm border-b-2 transition-colors ${
              tab === 'users'
                ? 'border-violet-500 text-zinc-100'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <Users className="h-4 w-4" />
            Users ({totalUsers})
          </button>
          <button
            onClick={() => setTab('usage')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm border-b-2 transition-colors ${
              tab === 'usage'
                ? 'border-violet-500 text-zinc-100'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <BarChart3 className="h-4 w-4" />
            Usage
          </button>
          <button
            onClick={() => setTab('plans')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm border-b-2 transition-colors ${
              tab === 'plans'
                ? 'border-violet-500 text-zinc-100'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <Settings2 className="h-4 w-4" />
            Plans
          </button>
          <button
            onClick={() => setTab('settings')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm border-b-2 transition-colors ${
              tab === 'settings'
                ? 'border-violet-500 text-zinc-100'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <Settings2 className="h-4 w-4" />
            Settings
          </button>
          <button
            onClick={() => setTab('integrations')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm border-b-2 transition-colors ${
              tab === 'integrations'
                ? 'border-violet-500 text-zinc-100'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <Plug className="h-4 w-4" />
            Integrations
          </button>
        </div>

        {loading && (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-zinc-500" />
          </div>
        )}

        {/* Users tab */}
        {!loading && tab === 'users' && (
          <div className="space-y-2">
            <CreateUserForm onCreated={loadUsers} config={config} />
            {users.map((u) => (
              <UserRow
                key={u.id}
                user={u}
                plans={plans}
                onUpdate={loadUsers}
                onDelete={handleDelete}
                currentUserId={currentUser.id}
                kioskMode={config.kiosk_mode}
              />
            ))}
            {users.length === 0 && (
              <p className="text-center text-sm text-zinc-500 py-8">No users found</p>
            )}
          </div>
        )}

        {/* Usage tab */}
        {!loading && tab === 'usage' && (
          <div className="space-y-6">
            {/* Summary cards */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {Object.entries(usageSummary).map(([type, count]) => (
                <div key={type} className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
                  <div className="text-2xl font-bold text-zinc-100">{count}</div>
                  <div className="text-xs text-zinc-500 mt-1">{type.replace(/_/g, ' ')}</div>
                </div>
              ))}
              {Object.keys(usageSummary).length === 0 && (
                <div className="col-span-full text-center text-sm text-zinc-500 py-4">
                  No usage data yet
                </div>
              )}
            </div>

            {/* Team spend (cost rollup) */}
            {costRollup && (
              <div>
                <div className="flex items-baseline gap-3 mb-3">
                  <h3 className="text-sm font-medium text-zinc-300">Team spend</h3>
                  <span className="text-lg font-semibold text-emerald-400">${costRollup.total_usd.toFixed(2)}</span>
                  <span className="text-xs text-zinc-500">total across all users</span>
                </div>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
                    <div className="px-3 py-2 text-xs font-medium text-zinc-400 border-b border-zinc-800">By user</div>
                    {costRollup.by_user.length === 0 && (
                      <p className="px-3 py-3 text-xs text-zinc-500">No costs recorded yet</p>
                    )}
                    {costRollup.by_user.map((u) => (
                      <div key={u.user_id} className="flex items-center gap-2 px-3 py-2 text-sm border-b border-zinc-800/50 last:border-0">
                        <span className="flex-1 truncate text-zinc-300">{u.display_name || u.email || u.user_id.slice(0, 8)}</span>
                        <span className="text-xs text-zinc-500">{u.runs} run{u.runs === 1 ? '' : 's'}</span>
                        <span className="w-16 text-right font-mono text-emerald-400">${(u.usd || 0).toFixed(2)}</span>
                      </div>
                    ))}
                  </div>
                  <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
                    <div className="px-3 py-2 text-xs font-medium text-zinc-400 border-b border-zinc-800">By run kind</div>
                    {costRollup.by_kind.length === 0 && (
                      <p className="px-3 py-3 text-xs text-zinc-500">No costs recorded yet</p>
                    )}
                    {costRollup.by_kind.map((k) => (
                      <div key={k.run_kind} className="flex items-center gap-2 px-3 py-2 text-sm border-b border-zinc-800/50 last:border-0">
                        <span className="flex-1 truncate text-zinc-300 capitalize">{k.run_kind || 'other'}</span>
                        <span className="text-xs text-zinc-500">{k.runs} run{k.runs === 1 ? '' : 's'}</span>
                        <span className="w-16 text-right font-mono text-emerald-400">${(k.usd || 0).toFixed(2)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* Recent logs */}
            <div>
              <h3 className="text-sm font-medium text-zinc-300 mb-3">Recent Activity</h3>
              <div className="space-y-1">
                {usageLogs.map((log) => (
                  <div key={log.id} className="flex items-center gap-3 px-3 py-2 rounded-md hover:bg-zinc-800/50 text-sm">
                    <span className="text-xs text-zinc-600 font-mono w-40 shrink-0">
                      {new Date(log.created_at).toLocaleString()}
                    </span>
                    <span className="px-2 py-0.5 rounded-full bg-zinc-800 text-zinc-400 text-xs">
                      {log.event_type}
                    </span>
                    <span className="text-zinc-500 text-xs truncate flex-1">
                      {(() => {
                        try {
                          const d = JSON.parse(log.detail)
                          return Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(', ')
                        } catch { return log.detail }
                      })()}
                    </span>
                    <span className="text-xs text-zinc-600 font-mono shrink-0">{log.user_id.slice(0, 8)}</span>
                  </div>
                ))}
                {usageLogs.length === 0 && (
                  <p className="text-center text-sm text-zinc-500 py-4">No activity logs yet</p>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Settings tab */}
        {!loading && tab === 'settings' && (
          <div className="space-y-4">
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-4">
              <h3 className="text-sm font-semibold text-zinc-200">Spawn Settings</h3>
              <label className="flex items-center justify-between cursor-pointer">
                <div>
                  <div className="text-sm text-zinc-300">Docker container spawning</div>
                  <div className="text-xs text-zinc-500 mt-0.5">Allow users to spawn agents as Docker containers. When disabled, only local process agents are available.</div>
                </div>
                <button
                  onClick={async () => {
                    const next = !config.docker_spawn_enabled
                    try {
                      await saveConfig({ docker_spawn_enabled: next })
                      setConfig({ ...config, docker_spawn_enabled: next })
                      // Update the auth store so the spawner page reacts immediately
                      useAuthStore.getState().setDockerSpawnEnabled(next)
                    } catch { setError('Failed to save config') }
                  }}
                  className={`relative inline-flex h-6 w-11 shrink-0 rounded-full border-2 border-transparent transition-colors ${
                    config.docker_spawn_enabled ? 'bg-violet-600' : 'bg-zinc-700'
                  }`}
                >
                  <span className={`inline-block h-5 w-5 rounded-full bg-white transition-transform ${
                    config.docker_spawn_enabled ? 'translate-x-5' : 'translate-x-0'
                  }`} />
                </button>
              </label>
            </div>

            {/* Kiosk mode (simple chat) */}
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-4">
              <div>
                <h3 className="flex items-center gap-2 text-sm font-semibold text-zinc-200">
                  <MessageSquare className="h-4 w-4 text-violet-400" />
                  Kiosk mode (simple chat)
                </h3>
                <p className="text-xs text-zinc-500 mt-0.5">
                  Locks users to the chat-first simple layout: they can chat, start an agent from an archetype,
                  manage their own connections and profile, and sign out. Admins are never locked.
                  A user already signed in picks up a change within about 15 minutes, or on reload.
                </p>
              </div>
              <div>
                <div className="inline-flex rounded-md border border-zinc-700 p-0.5" role="radiogroup" aria-label="Kiosk mode">
                  {KIOSK_MODES.map((m) => (
                    <button
                      key={m.value}
                      type="button"
                      role="radio"
                      aria-checked={config.kiosk_mode === m.value}
                      onClick={async () => {
                        if (config.kiosk_mode === m.value && !config.kiosk_mode_from_env) return
                        try {
                          const next = await saveConfig({ kiosk_mode: m.value })
                          setConfig(next)
                          useAuthStore.getState().setSimpleChatOnly(next.kiosk_mode === 'all')
                        } catch { setError('Failed to save config') }
                      }}
                      className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
                        config.kiosk_mode === m.value
                          ? 'bg-violet-600 text-white'
                          : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200'
                      }`}
                    >
                      {m.label}
                    </button>
                  ))}
                </div>
                <p className="mt-1.5 text-xs text-zinc-500">
                  {KIOSK_MODES.find((m) => m.value === config.kiosk_mode)?.hint}
                  {config.kiosk_mode_from_env && ' Set by --simple-chat until you pick a mode here.'}
                </p>
              </div>
              <label className="flex items-center justify-between gap-4">
                <div>
                  <div className="text-sm text-zinc-300">New users start in kiosk mode</div>
                  <div className="text-xs text-zinc-500 mt-0.5">
                    Marks accounts created from now on (Add user or sign-up). Counts under "Selected users";
                    with "Everyone" every non-admin is locked anyway.
                  </div>
                </div>
                <Switch
                  on={config.kiosk_new_users}
                  label="New users start in kiosk mode"
                  onToggle={async () => {
                    try {
                      setConfig(await saveConfig({ kiosk_new_users: !config.kiosk_new_users }))
                    } catch { setError('Failed to save config') }
                  }}
                />
              </label>
            </div>

            {/* Provider API Keys */}
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-4">
              <div>
                <h3 className="text-sm font-semibold text-zinc-200">Provider API Keys</h3>
                <p className="text-xs text-zinc-500 mt-0.5">System-wide API keys for LLM providers. These are used as defaults when spawning agents.</p>
              </div>
              {LLM_PROVIDERS.map((p) => (
                <div key={p.value}>
                  <label className="mb-1.5 flex items-center gap-2 text-sm font-medium text-zinc-300">
                    <Key className="h-3.5 w-3.5 text-zinc-500" />
                    {p.label}
                    {providerKeys[p.value] && <span className="text-xs text-emerald-500/70 font-normal">configured</span>}
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="password"
                      value={providerKeysDraft[p.value] || ''}
                      onChange={(e) => setProviderKeysDraft({ ...providerKeysDraft, [p.value]: e.target.value })}
                      placeholder={providerKeys[p.value] ? '••••••••' : `${p.label} API key`}
                      className="flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-xs font-mono text-zinc-200 focus:border-violet-500/50 focus:outline-none"
                    />
                    <button
                      onClick={async () => {
                        try {
                          const val = providerKeysDraft[p.value] || ''
                          const updated = await saveProviderKeysApi({ [p.value]: val })
                          setProviderKeys(updated)
                          setProviderKeysDraft(updated)
                          setKeySaveFlash(p.value)
                          setTimeout(() => setKeySaveFlash(null), 1500)
                        } catch { setError('Failed to save provider key') }
                      }}
                      className={`flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
                        keySaveFlash === p.value
                          ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400'
                          : 'border-zinc-700 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200'
                      }`}
                    >
                      {keySaveFlash === p.value ? <Check className="h-3 w-3" /> : <Save className="h-3 w-3" />}
                      {keySaveFlash === p.value ? 'Saved' : 'Save'}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Owner profile defaults every user's agents receive. Not behind
            `loading`: it loads on its own, and a Refresh must not unmount it
            and drop a long unsaved draft. */}
        {tab === 'settings' && <DeckProfileDefaults />}

        {/* Set up once, used by everyone: the team Telegram bot (with its
            access requests) and the shared Google / Microsoft / Jev credentials.
            They load on their own, like the profile defaults above. */}
        {tab === 'integrations' && (
          <div className="space-y-4">
            <SharedTelegramBotAdmin refreshTick={refreshTick} />
            <SharedCredentialsAdmin refreshTick={refreshTick} />
          </div>
        )}

        {/* Plans tab */}
        {!loading && tab === 'plans' && (
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            {Object.entries(plans).map(([name, limits]) => (
              <PlanCard
                key={name}
                name={name}
                limits={limits}
                onSave={async (plan, newLimits) => {
                  await updatePlan(plan, newLimits)
                  await loadUsers()
                }}
              />
            ))}
            {Object.keys(plans).length === 0 && (
              <p className="col-span-full text-center text-sm text-zinc-500 py-8">No plans configured</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
