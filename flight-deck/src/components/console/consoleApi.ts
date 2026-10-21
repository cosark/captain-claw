import { adminHeaders, type PlanLimits } from '../admin/deckConfig'

// The admin endpoints the console's pages read and write. The Admin page
// (pages/AdminPage.tsx) calls the same ones; these are the console's copies.

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: adminHeaders(), credentials: 'include' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : `${res.status} ${res.statusText}`)
  }
  return res.json()
}

// ── Team keys: one per provider, for its own endpoint (fd:provider-keys) ──

export const TEAM_KEY_PROVIDERS = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'gemini', label: 'Gemini' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'xai', label: 'xAI' },
  { value: 'litert', label: 'LiteRT (local Gemma)' },
] as const

/** Masked values ('' = none) by provider. */
export async function fetchProviderKeys(): Promise<Record<string, string>> {
  try {
    return (await call<{ keys?: Record<string, string> }>('/fd/admin/provider-keys')).keys || {}
  } catch { return {} }
}

export async function saveProviderKey(provider: string, key: string): Promise<Record<string, string>> {
  const data = await call<{ keys?: Record<string, string> }>('/fd/admin/provider-keys', {
    method: 'PUT', body: JSON.stringify({ keys: { [provider]: key } }),
  })
  return data.keys || {}
}

// ── People ──

export interface AdminUser {
  id: string
  email: string
  display_name: string
  role: string
  created_at: string
  metadata: string
}

export async function fetchAllUsers(): Promise<{ users: AdminUser[]; total: number }> {
  return call('/fd/admin/users?limit=1000&offset=0')
}

export async function fetchWaitlistPending(): Promise<number> {
  const data = await call<{ counts?: { pending?: number } }>('/fd/admin/waitlist?status=pending')
  return data.counts?.pending ?? 0
}

export async function fetchOpenInvites(): Promise<number> {
  const data = await call<{ invites: { status: string; expired?: boolean }[] }>('/fd/admin/invites?status=pending')
  return data.invites.filter((i) => i.status === 'pending' && !i.expired).length
}

// ── Usage ──

export interface DayUsage { messages: number; tokens: number }

export interface UsageToday {
  day: string
  resets_at: string
  usage: Record<string, DayUsage>
  resets: Record<string, number>
  reset_cap: number
}

export const fetchUsageToday = () => call<UsageToday>('/fd/admin/usage/today')

export interface CostRollup {
  by_user: { user_id: string; email: string | null; display_name: string | null; runs: number; usd: number }[]
  by_kind: { run_kind: string; runs: number; usd: number }[]
  total_usd: number
}

export function fetchCosts(since?: string): Promise<CostRollup> {
  return call(`/fd/admin/costs${since ? `?since=${encodeURIComponent(since)}` : ''}`)
}

export interface UsageLog {
  id: number
  user_id: string
  event_type: string
  detail: string
  created_at: string
}

export async function fetchUsageLogs(limit = 40): Promise<UsageLog[]> {
  return (await call<{ logs: UsageLog[] }>(`/fd/admin/usage?limit=${limit}`)).logs
}

export async function fetchUsageSummary(since?: string): Promise<Record<string, number>> {
  return (await call<{ summary: Record<string, number> }>(
    `/fd/admin/usage/summary${since ? `?since=${encodeURIComponent(since)}` : ''}`,
  )).summary
}

// ── Plans ──

export async function updatePlan(plan: string, limits: Partial<PlanLimits>): Promise<void> {
  await call(`/fd/admin/plans/${plan}`, { method: 'PUT', body: JSON.stringify(limits) })
}
