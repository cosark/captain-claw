import { useCallback, useEffect, useState } from 'react'
import { Send, Loader2, Check, X, Trash2, AlertCircle, Save } from 'lucide-react'
import {
  getAdminTelegram,
  saveAdminTelegram,
  clearAdminTelegram,
  decideTelegramLink,
  removeTelegramLink,
  type AdminTelegram,
  type AdminTelegramLink,
} from '../../services/sharedIntegrations'

// Admin → Integrations: the deck's shared Telegram bot. The admin sets one bot
// token; users ask for it in Connections → Telegram with one of their agents;
// requests land here to approve. Flight Deck polls the bot and relays each
// approved user's chat to their agent (captain_claw/flight_deck/telegram_shared.py).

function when(iso: string | null): string {
  if (!iso) return ''
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

function tgAccount(l: AdminTelegramLink): string {
  if (!l.linked) return 'Telegram not linked yet'
  return l.tg_username ? `@${l.tg_username}` : (l.tg_first_name || 'linked')
}

export function SharedTelegramBotAdmin({ refreshTick = 0 }: { refreshTick?: number }) {
  const [data, setData] = useState<AdminTelegram | null>(null)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [flash, setFlash] = useState('')

  const load = useCallback(async () => {
    try {
      setData(await getAdminTelegram())
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => { load() }, [load, refreshTick])
  // Requests and the poller's health change on their own: keep the card fresh.
  useEffect(() => {
    const t = window.setInterval(load, 15000)
    return () => window.clearInterval(t)
  }, [load])

  const run = async (fn: () => Promise<AdminTelegram>, done = '') => {
    setBusy(true)
    setError('')
    try {
      setData(await fn())
      if (done) {
        setFlash(done)
        window.setTimeout(() => setFlash(''), 2000)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const cfg = data?.config
  const pending = data?.links.filter((l) => l.status === 'pending') ?? []
  const approved = data?.links.filter((l) => l.status === 'approved') ?? []
  const rejected = data?.links.filter((l) => l.status === 'rejected') ?? []

  const row = (l: AdminTelegramLink) => (
    <div key={l.user_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-sm text-zinc-200 truncate">{l.user_name || l.user_email}</div>
        <div className="text-xs text-zinc-500">
          {l.user_name && <span>{l.user_email} · </span>}
          agent <span className="text-zinc-300">{l.agent_name}</span>
          {l.status === 'approved' && l.agent_online === false && <span className="text-amber-500"> (not running)</span>}
          {' · '}{tgAccount(l)}
          {l.requested_at && <span> · asked {when(l.requested_at)}</span>}
        </div>
      </div>
      <div className="flex items-center gap-1.5">
        {l.status !== 'approved' && (
          <button
            onClick={() => run(() => decideTelegramLink(l.user_id, 'approve'))}
            disabled={busy}
            className="flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-xs text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            <Check className="h-3 w-3" /> Approve
          </button>
        )}
        {l.status === 'pending' && (
          <button
            onClick={() => run(() => decideTelegramLink(l.user_id, 'reject'))}
            disabled={busy}
            className="flex items-center gap-1 rounded-md bg-zinc-800 px-2.5 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:opacity-50"
          >
            <X className="h-3 w-3" /> Reject
          </button>
        )}
        {l.status !== 'pending' && (
          <button
            onClick={() => {
              if (confirm(`Remove ${l.user_name || l.user_email}'s access to the team bot?`)) {
                run(() => removeTelegramLink(l.user_id))
              }
            }}
            disabled={busy}
            title="Remove access"
            className="rounded-md p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-red-400 disabled:opacity-50"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  )

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 space-y-4">
      <div className="flex items-start gap-3">
        <Send className="h-4 w-4 text-sky-400 mt-0.5" />
        <div className="flex-1">
          <h3 className="text-sm font-semibold text-zinc-200">Team Telegram bot</h3>
          <p className="text-xs text-zinc-500 mt-0.5">
            One bot for everyone on this deck. People ask for it in Connections → Telegram, picking
            which of their own agents answers them; you approve here. Flight Deck holds the token and
            passes each person's messages to their agent — the chats themselves stay in the agent.
            Agents with a bot of their own keep it.
          </p>
        </div>
      </div>

      {data && !data.auth_enabled && (
        <div className="rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs text-zinc-400">
          The team bot needs Flight Deck sign-in, which is off on this deck.
        </div>
      )}

      {cfg?.configured && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span className="text-zinc-200 font-medium">
            {cfg.bot_username ? `@${cfg.bot_username}` : 'Bot'}{cfg.bot_name ? ` · ${cfg.bot_name}` : ''}
          </span>
          <span className="font-mono text-zinc-500">token {cfg.token_hint}</span>
          {!cfg.enabled ? (
            <span className="text-zinc-400">off</span>
          ) : data?.status.running && !data.status.error ? (
            <span className="text-emerald-500">listening</span>
          ) : (
            <span className="text-amber-500">starting…</span>
          )}
          <label className="ml-auto flex items-center gap-2 text-zinc-400">
            <input
              type="checkbox"
              checked={cfg.enabled}
              disabled={busy}
              onChange={(e) => run(() => saveAdminTelegram({ enabled: e.target.checked }))}
              className="h-3.5 w-3.5 accent-violet-600"
            />
            On
          </label>
        </div>
      )}

      {cfg?.configured && (
        <label className="flex items-start justify-between gap-4 rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2.5">
          <div>
            <div className="text-sm text-zinc-300">Approve requests automatically</div>
            <div className="text-xs text-zinc-500 mt-0.5">
              Anyone on this deck who asks gets the bot at once, with their own agent. Someone you turned
              down still waits for you. Switching it on also approves the requests waiting now.
            </div>
          </div>
          <input
            type="checkbox"
            role="switch"
            checked={cfg.auto_approve}
            disabled={busy}
            onChange={(e) => {
              const on = e.target.checked
              if (on && pending.length > 0
                && !confirm(`Approve requests automatically? The ${pending.length} waiting now ${pending.length === 1 ? 'is' : 'are'} approved too.`)) {
                return
              }
              run(() => saveAdminTelegram({ auto_approve: on }))
            }}
            className="mt-1 h-4 w-4 shrink-0 accent-violet-600"
          />
        </label>
      )}

      {cfg?.configured && (
        <label className="flex items-start justify-between gap-4 rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2.5">
          <div>
            <div className="text-sm text-zinc-300">Telegram inbox</div>
            <div className="text-xs text-zinc-500 mt-0.5">
              Approved people may let their agents read their Telegram through this bot — the private
              chats they connect in Telegram's Chat Automation and groups they add the bot to.
              Read-only, each person's own consent, kept encrypted on this deck. Switching it off
              stops collecting; what's kept expires with each person's own retention.
            </div>
            {cfg.inbox && data?.status.bot_flags && (
              <div className="text-xs mt-1.5 space-y-0.5">
                <div className={data.status.bot_flags.business ? 'text-emerald-500' : 'text-amber-500'}>
                  {data.status.bot_flags.business === null ? 'Business Mode: not checked yet'
                    : data.status.bot_flags.business ? 'Business Mode on — private chats can be connected'
                    : 'Business Mode off — turn it on in @BotFather → Bot Settings → Business Mode, or private chats can\'t be connected'}
                </div>
                <div className={data.status.bot_flags.groups ? 'text-emerald-500' : 'text-amber-500'}>
                  {data.status.bot_flags.groups === null ? 'Group privacy: not checked yet'
                    : data.status.bot_flags.groups ? 'Group privacy off — the bot sees every group message'
                    : 'Group privacy on — in groups the bot only sees commands. Turn it off in @BotFather → Bot Settings → Group Privacy (then re-add the bot), or make it a group admin'}
                </div>
              </div>
            )}
          </div>
          <input
            type="checkbox"
            role="switch"
            checked={cfg.inbox}
            disabled={busy}
            onChange={(e) => run(() => saveAdminTelegram({ inbox: e.target.checked }))}
            className="mt-1 h-4 w-4 shrink-0 accent-violet-600"
          />
        </label>
      )}

      {data?.status.error && cfg?.enabled && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          {data.status.error}
        </div>
      )}

      <div>
        <label className="mb-1.5 block text-xs font-medium text-zinc-400">
          {cfg?.configured ? 'Replace the bot token' : 'Bot token'}
        </label>
        <div className="flex gap-2">
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={cfg?.configured ? '••••••••' : '123456789:AA…  (from @BotFather → /newbot)'}
            className="flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-xs font-mono text-zinc-200 focus:border-violet-500/50 focus:outline-none"
          />
          <button
            onClick={() => run(async () => {
              const out = await saveAdminTelegram({ token })
              setToken('')
              return out
            }, 'Saved')}
            disabled={busy || !token.trim()}
            className="flex items-center gap-1 rounded-md border border-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
            {flash || 'Save'}
          </button>
          {cfg?.configured && (
            <button
              onClick={() => {
                if (confirm('Remove the bot token? The team bot stops; requests and approvals are kept for a new token.')) {
                  run(clearAdminTelegram)
                }
              }}
              disabled={busy}
              className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-red-400 disabled:opacity-50"
            >
              Remove
            </button>
          )}
        </div>
        <p className="mt-1 text-[11px] text-zinc-500">
          Use a bot nothing else reads — not one an agent already has as its own. Flight Deck checks
          the token with Telegram before saving it.
        </p>
      </div>

      {data && data.links.length > 0 && (
        <div className="space-y-3">
          {pending.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-xs font-medium text-amber-500">Waiting for approval ({pending.length})</div>
              {pending.map(row)}
            </div>
          )}
          {approved.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-xs font-medium text-zinc-400">Approved ({approved.length})</div>
              {approved.map(row)}
            </div>
          )}
          {rejected.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-xs font-medium text-zinc-500">Not approved ({rejected.length})</div>
              {rejected.map(row)}
            </div>
          )}
        </div>
      )}
      {data && data.links.length === 0 && cfg?.configured && (
        <div className="text-xs text-zinc-500">No requests yet.</div>
      )}

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
          {error}
        </div>
      )}
    </div>
  )
}
