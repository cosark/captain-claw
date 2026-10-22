import { useEffect, useState } from 'react'
import {
  LayoutGrid, Users, ClipboardList, Boxes, MessageSquare, DollarSign, CheckCircle2, Circle,
  ChevronRight, Send, Server, LayoutDashboard, UserPlus, Library, Gauge,
} from 'lucide-react'
import { useAuthStore, DEFAULT_INSTANCE_NAME } from '../../stores/authStore'
import { useConsoleStore, leaveConsole, type ConsolePage, type ConsoleIntent } from '../../stores/consoleStore'
import { fetchSharedTierSets } from '../../services/sharedTierSets'
import { getOAuthClientConfig, getAdminTelegram } from '../../services/sharedIntegrations'
import { getSystemProcesses, type SystemProcessResponse } from '../../services/system'
import {
  fetchAllUsers, fetchWaitlistPending, fetchOpenInvites, fetchUsageToday, fetchCosts, fetchProviderKeys,
} from './consoleApi'
import { Page, PageHeader, Card, StatTile, Button, Notice } from './parts'
import { fmtNumber } from './format'

// Overview: how the deck is doing at a glance — people, what's waiting for
// you, today's use and spend, the machine — and what's left to set up.

interface Facts {
  users?: number
  admins?: number
  waitlist?: number
  invites?: number
  messagesToday?: number
  tokensToday?: number
  spend30?: number
  teamModels?: string | null
  teamKeys?: number
  googleReady?: boolean
  telegramRequests?: number
  system?: SystemProcessResponse | null
}

function greeting(): string {
  const h = new Date().getHours()
  return h < 5 ? 'Good evening' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

export function OverviewPage() {
  const go = useConsoleStore((s) => s.go)
  const user = useAuthStore((s) => s.user)
  const branding = useAuthStore((s) => s.branding)
  // A deck without sign-in (the desktop app) has one user: no team to run.
  const accounts = useAuthStore((s) => s.authEnabled !== false)
  const [f, setF] = useState<Facts>({})
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    const put = (patch: Facts) => { if (!cancelled) setF((prev) => ({ ...prev, ...patch })) }
    const month = new Date(Date.now() - 30 * 86400_000).toISOString()
    const team = accounts ? [
      fetchAllUsers().then((r) => put({ users: r.total ?? r.users.length, admins: r.users.filter((u) => u.role === 'admin').length })),
      fetchWaitlistPending().then((n) => put({ waitlist: n })),
      fetchOpenInvites().then((n) => put({ invites: n })),
      fetchSharedTierSets().then((r) => put({ teamModels: r.sets[0]?.name ?? null })),
      getAdminTelegram().then((t) => put({ telegramRequests: t.links.filter((l) => l.status === 'pending').length })),
    ] : []
    Promise.allSettled([
      ...team,
      fetchUsageToday().then((t) => {
        const all = Object.values(t.usage)
        put({ messagesToday: all.reduce((s, u) => s + u.messages, 0), tokensToday: all.reduce((s, u) => s + u.tokens, 0) })
      }),
      fetchCosts(month).then((c) => put({ spend30: c.total_usd })),
      fetchProviderKeys().then((k) => put({ teamKeys: Object.values(k).filter(Boolean).length })),
      getOAuthClientConfig('google').then((c) => put({ googleReady: !!c.client_id_set && !!c.client_secret_set })),
      getSystemProcesses().then((s) => put({ system: s })).catch(() => put({ system: null })),
    ]).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [accounts])

  const named = branding.name !== DEFAULT_INSTANCE_NAME || !!branding.logo
  const allSteps: { done: boolean; title: string; detail: string; page: ConsolePage; intent?: ConsoleIntent; team?: boolean }[] = [
    {
      done: !!f.teamModels, page: 'models', team: true,
      title: 'Give the team its models',
      detail: f.teamModels ? `The team runs on “${f.teamModels}”.` : 'Publish a tier set — everyone without models of their own runs on it.',
    },
    {
      done: (f.teamKeys ?? 0) > 0, page: 'models',
      title: accounts ? 'Add the team\'s API keys' : 'Add your API keys',
      detail: f.teamKeys ? `${f.teamKeys} provider key${f.teamKeys === 1 ? '' : 's'} set.`
        : accounts ? 'One key per provider, so people never handle keys themselves.' : 'One key per provider, for every agent you start.',
    },
    {
      done: !!f.googleReady, page: 'connections', intent: { connection: 'google' },
      title: accounts ? 'Let people connect Google' : 'Connect Google',
      detail: f.googleReady ? 'Gmail, Calendar and Drive are one click away.' : 'Add the deck\'s Google OAuth client once.',
    },
    {
      done: named, page: 'settings',
      title: 'Name the deck',
      detail: named ? `People see “${branding.name}”.` : 'Its name and logo on the sign-in page and every panel.',
    },
    {
      done: (f.users ?? 0) > 1 || (f.invites ?? 0) > 0, page: 'users', team: true,
      title: 'Bring in your team',
      detail: (f.users ?? 0) > 1 ? `${f.users} people have an account.` : 'Invite people by email or add their accounts.',
    },
  ]
  const steps = allSteps.filter((s) => accounts || !s.team)
  const doneCount = steps.filter((s) => s.done).length

  const host = f.system?.available ? f.system.host : null
  const memPct = host?.mem_percent ?? null
  const diskPct = host?.disk_total_gb && host.disk_free_gb !== null
    ? Math.round(((host.disk_total_gb - host.disk_free_gb) / host.disk_total_gb) * 100) : null

  return (
    <Page>
      <PageHeader
        icon={LayoutGrid}
        title={`${greeting()}${user?.display_name ? `, ${user.display_name.split(' ')[0]}` : ''}`}
        description={<>Here's how <b className="font-medium text-zinc-300">{branding.name}</b> is doing.</>}
        actions={<Button icon={LayoutDashboard} onClick={() => leaveConsole('desktop')}>Open Flight Deck</Button>}
      />

      {((f.waitlist ?? 0) > 0 || (f.telegramRequests ?? 0) > 0) && (
        <div className="space-y-2">
          {(f.waitlist ?? 0) > 0 && (
            <AttentionRow
              icon={ClipboardList}
              text={`${f.waitlist} ${f.waitlist === 1 ? 'person is' : 'people are'} waiting for approval on the waitlist`}
              action="Review"
              onClick={() => go('users', { usersTab: 'waitlist' })}
            />
          )}
          {(f.telegramRequests ?? 0) > 0 && (
            <AttentionRow
              icon={Send}
              text={`${f.telegramRequests} ${f.telegramRequests === 1 ? 'person asked' : 'people asked'} to use the team's Telegram bot`}
              action="Review"
              onClick={() => go('connections', { connection: 'telegram' })}
            />
          )}
        </div>
      )}

      <div className={`grid grid-cols-2 gap-3 ${accounts ? 'lg:grid-cols-4' : 'lg:grid-cols-3'}`}>
        {accounts && (
          <StatTile
            icon={Users} tone="violet" label="People" value={f.users ?? '—'} loading={loading && f.users === undefined}
            sub={f.users !== undefined ? `${f.admins ?? 0} admin${f.admins === 1 ? '' : 's'}${f.invites ? ` · ${f.invites} invited` : ''}` : undefined}
            onClick={() => go('users')}
          />
        )}
        <StatTile
          icon={Boxes} tone="emerald" label="Agents running"
          value={f.system?.available ? f.system.summary.agents : '—'}
          loading={loading && f.system === undefined}
          sub={f.system?.available && f.system.summary.stopped ? `${f.system.summary.stopped} stopped` : accounts ? 'across every user' : undefined}
        />
        <StatTile
          icon={MessageSquare} tone="cyan" label="Messages today" value={f.messagesToday !== undefined ? fmtNumber(f.messagesToday) : '—'}
          loading={loading && f.messagesToday === undefined}
          sub={f.tokensToday !== undefined ? `${fmtNumber(f.tokensToday)} AI tokens` : undefined}
          onClick={accounts ? () => go('usage') : undefined}
        />
        <StatTile
          icon={DollarSign} tone="amber" label="Spend, 30 days" value={f.spend30 !== undefined ? `$${f.spend30.toFixed(2)}` : '—'}
          loading={loading && f.spend30 === undefined}
          sub="on models"
          onClick={accounts ? () => go('usage') : undefined}
        />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <Card
          className="lg:col-span-3"
          title="Set up the deck"
          hint={doneCount === steps.length ? `All done — the deck is ready${accounts ? ' for your team' : ''}.` : `${doneCount} of ${steps.length} done`}
          action={<div className="h-1.5 w-24 overflow-hidden rounded-full bg-zinc-800"><div className="h-full rounded-full bg-emerald-500" style={{ width: `${(doneCount / steps.length) * 100}%` }} /></div>}
          bodyClassName="divide-y divide-zinc-800/70"
        >
          {steps.map((s) => (
            <button
              key={s.title}
              onClick={() => go(s.page, s.intent)}
              className="group flex w-full items-center gap-3 px-5 py-3.5 text-left transition-colors hover:bg-zinc-800/30"
            >
              {s.done
                ? <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-500" />
                : <Circle className="h-5 w-5 shrink-0 text-zinc-600" />}
              <span className="min-w-0 flex-1">
                <span className={`block text-sm font-medium ${s.done ? 'text-zinc-400' : 'text-zinc-100'}`}>{s.title}</span>
                <span className="block truncate text-xs text-zinc-500">{s.detail}</span>
              </span>
              <ChevronRight className="h-4 w-4 shrink-0 text-zinc-600 group-hover:text-zinc-300" />
            </button>
          ))}
        </Card>

        <div className="space-y-6 lg:col-span-2">
          <Card icon={Server} title="This machine" hint={host ? `${host.cpu_count ?? '?'} CPUs` : undefined}>
            {f.system === undefined ? (
              <p className="text-sm text-zinc-500">Loading…</p>
            ) : !host ? (
              <p className="text-sm text-zinc-500">The machine's numbers aren't available here.</p>
            ) : (
              <div className="space-y-4">
                <Meter label="Load" value={host.load_avg ? host.load_avg[0].toFixed(2) : '—'} pct={host.load_avg && host.cpu_count ? Math.round((host.load_avg[0] / host.cpu_count) * 100) : null} />
                <Meter
                  label="Memory"
                  value={host.mem_used_mb && host.mem_total_mb ? `${(host.mem_used_mb / 1024).toFixed(1)} of ${(host.mem_total_mb / 1024).toFixed(0)} GB` : '—'}
                  pct={memPct}
                />
                <Meter
                  label="Disk"
                  value={host.disk_free_gb !== null ? `${host.disk_free_gb.toFixed(0)} GB free` : '—'}
                  pct={diskPct}
                />
              </div>
            )}
          </Card>

          <Card title="Quick actions" bodyClassName="grid grid-cols-1 gap-2 p-4">
            {accounts && <QuickAction icon={UserPlus} label="Invite someone" onClick={() => go('users', { usersTab: 'invitations' })} />}
            <QuickAction icon={Library} label="New archetype" onClick={() => go('archetypes')} />
            <QuickAction icon={Gauge} label={accounts ? 'Change the team\'s models' : 'Change your models'} onClick={() => go('models')} />
          </Card>
        </div>
      </div>

      {!loading && f.system === null && (
        <Notice>Agent and machine numbers come from the Processes page, which this deck doesn't serve.</Notice>
      )}
    </Page>
  )
}

function AttentionRow({ icon: Icon, text, action, onClick }: { icon: typeof Users; text: string; action: string; onClick: () => void }) {
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-violet-500/30 bg-violet-500/[0.06] px-4 py-3">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-violet-500/15 text-violet-600 dark:text-violet-300">
        <Icon className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1 text-sm text-zinc-200">{text}</span>
      <Button variant="primary" onClick={onClick}>{action}</Button>
    </div>
  )
}

function Meter({ label, value, pct }: { label: string; value: string; pct: number | null }) {
  const tone = pct === null ? 'bg-zinc-600' : pct >= 90 ? 'bg-red-500' : pct >= 75 ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="text-zinc-400">{label}</span>
        <span className="tabular-nums text-zinc-200">{value}</span>
      </div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-zinc-800">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct === null ? 0 : Math.min(100, Math.max(2, pct))}%` }} />
      </div>
    </div>
  )
}

function QuickAction({ icon: Icon, label, onClick }: { icon: typeof Users; label: string; onClick: () => void }) {
  return (
    <button onClick={onClick} className="group flex items-center gap-3 rounded-xl border border-zinc-800 px-3 py-2.5 text-left text-sm text-zinc-300 transition-colors hover:border-violet-500/40 hover:bg-zinc-800/40 hover:text-zinc-100">
      <Icon className="h-4 w-4 text-zinc-500 group-hover:text-violet-500" />
      <span className="flex-1">{label}</span>
      <ChevronRight className="h-4 w-4 text-zinc-600" />
    </button>
  )
}
