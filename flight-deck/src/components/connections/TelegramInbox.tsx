import { useCallback, useEffect, useState } from 'react'
import { Inbox, Loader2, Trash2, AlertCircle, Check, ChevronRight } from 'lucide-react'
import {
  getTelegramInbox,
  setTelegramInbox,
  clearTelegramInbox,
  removeInboxGroup,
  getTelegramInboxRecent,
  type TelegramInbox as InboxView,
  type InboxMessage,
} from '../../services/sharedIntegrations'

// The Telegram inbox (captain_claw/flight_deck/telegram_inbox.py): a read-only
// copy of the user's Telegram that their agents read, like mail — the private
// chats they connect to the team bot in Telegram → Settings → Chat Automation
// (Telegram 12.7+; with Premium also Settings → Telegram Business → Chatbots),
// and groups they add the bot to. Off until the user consents here; turning it
// off deletes everything kept. Nothing is ever sent in the user's name.

function day(iso: string | null): string {
  if (!iso) return ''
  try { return new Date(iso).toLocaleDateString() } catch { return iso }
}

function time(iso: string | null): string {
  if (!iso) return ''
  try { return new Date(iso).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) } catch { return iso }
}

export default function TelegramInbox({ linkKey }: { linkKey: string }) {
  const [view, setView] = useState<InboxView | null>(null)
  const [consent, setConsent] = useState(false)
  const [days, setDays] = useState(30)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [recent, setRecent] = useState<InboxMessage[] | null>(null)

  const apply = useCallback((v: InboxView) => {
    setView(v)
    setDays(v.retention_days)
  }, [])

  const load = useCallback(async () => {
    try {
      apply(await getTelegramInbox())
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [apply])

  // Reload when the Telegram link above changes (linked, approved, removed).
  useEffect(() => { load() }, [load, linkKey])

  // Waiting for the Chat Automation connection: look again now and then.
  const waiting = !!view?.enabled && !view.connection
  useEffect(() => {
    if (!waiting) return
    const t = window.setInterval(() => { getTelegramInbox().then(apply).catch(() => {}) }, 10000)
    return () => window.clearInterval(t)
  }, [waiting, apply])

  const run = async (fn: () => Promise<InboxView>) => {
    setBusy(true)
    setError('')
    try {
      apply(await fn())
      if (recent) setRecent((await getTelegramInboxRecent()).messages)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const toggleRecent = async () => {
    if (recent) { setRecent(null); return }
    try {
      setRecent((await getTelegramInboxRecent()).messages)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  if (!view) return null
  const bot = view.bot_username ? `@${view.bot_username}` : 'the team bot'
  const box = 'rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2.5 text-xs text-zinc-300'
  const field =
    'rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 focus:border-violet-500/50 focus:outline-none disabled:opacity-50'
  const retention = (
    <select
      value={days}
      disabled={busy}
      onChange={(e) => {
        const d = Number(e.target.value)
        setDays(d)
        if (view.enabled) run(() => setTelegramInbox({ retention_days: d }))
      }}
      className={field}
    >
      {view.retention_choices.map((d) => (
        <option key={d} value={d}>{d === 365 ? '1 year' : `${d} days`}</option>
      ))}
    </select>
  )

  return (
    <div className="border-t border-zinc-800 pt-4 space-y-3">
      <div className="flex items-center gap-2">
        <Inbox className="h-4 w-4 text-sky-400" />
        <h4 className="text-sm font-semibold text-zinc-100">Inbox — let your agents read your Telegram</h4>
        {view.enabled && (
          <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border bg-emerald-500/15 text-emerald-600 dark:text-emerald-500 border-emerald-500/30">
            <Check className="h-3 w-3" /> On
          </span>
        )}
      </div>

      {!view.available ? (
        <div className="text-xs text-zinc-500">{view.reason}</div>
      ) : !view.enabled ? (
        <>
          <p className="text-xs text-zinc-400">
            Your agents can read your Telegram chats the way they read your email — to know what's
            going on, answer questions about it and remind you. They only read: nothing is ever sent,
            replied to or marked read in your name.
          </p>
          {!view.linked ? (
            <div className="text-xs text-zinc-500">Connect your Telegram account to the team bot above first.</div>
          ) : (
            <div className={box + ' space-y-2.5'}>
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  checked={consent}
                  onChange={(e) => setConsent(e.target.checked)}
                  className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-violet-600"
                />
                <span>
                  I agree that Flight Deck keeps a copy of the messages in the chats I connect — mine and
                  other people's — encrypted on this deck for {retention}, that my agents read them, and
                  that what they read goes to the AI model they use. Turning the inbox off deletes it all.
                </span>
              </label>
              <button
                onClick={() => run(() => setTelegramInbox({ enabled: true, consent: true, retention_days: days }))}
                disabled={busy || !consent}
                className="flex items-center gap-1.5 rounded-md bg-violet-600 px-3 py-1.5 text-xs text-white hover:bg-violet-500 disabled:opacity-50"
              >
                {busy && <Loader2 className="h-3 w-3 animate-spin" />} Turn on inbox
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          <div className={box + ' space-y-1.5'}>
            <div className="font-medium text-zinc-100">Private chats</div>
            {view.connection ? (
              <>
                <div>
                  Connected{view.connection.connected_at ? ` since ${day(view.connection.connected_at)}` : ''}.
                  Change which chats it sees where you connected it in Telegram (Settings → Chat
                  Automation, or Settings → Telegram Business → Chatbots).
                </div>
                {view.connection.rights.length > 0 && (
                  <div className="text-zinc-500">
                    You gave the bot permissions it never uses ({view.connection.rights.join(', ')}) —
                    you can switch them off there.
                  </div>
                )}
              </>
            ) : (
              <ol className="list-decimal pl-4 space-y-0.5 text-zinc-400">
                <li>
                  In Telegram open <span className="text-zinc-200">Settings → Chat Automation</span>. It
                  needs Telegram 12.7 or newer — if it isn't there, update the app (or try the phone
                  app). With Telegram Premium you can use{' '}
                  <span className="text-zinc-200">Settings → Telegram Business → Chatbots</span> instead.
                </li>
                <li>Choose <span className="text-zinc-200">{bot}</span> and pick which chats it may see
                  (all, only new ones, everyone but your contacts …).</li>
                <li>Leave every permission off — it only reads. Messages from then on reach the inbox;
                  older history isn't available.</li>
              </ol>
            )}
            {view.bot.business === false && (
              <div className="flex items-start gap-1.5 text-amber-600 dark:text-amber-400">
                <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-px" />
                The team bot can't be connected to accounts yet — an admin has to turn on Secretary
                Mode for it in @BotFather.
              </div>
            )}
          </div>

          <div className={box + ' space-y-1.5'}>
            <div className="font-medium text-zinc-100">Groups</div>
            <div className="text-zinc-400">
              Add {bot} to a group and it keeps that group for you — it tells the members once and never
              posts. Already there? Send <span className="font-mono text-zinc-200">/inbox</span> in the
              group; <span className="font-mono text-zinc-200">/inbox off</span> stops.
            </div>
            {view.bot.groups === false && (
              <div className="flex items-start gap-1.5 text-amber-600 dark:text-amber-400">
                <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-px" />
                The bot's group privacy is on, so it only sees commands in groups. Make it a group admin,
                or ask an admin to turn group privacy off in @BotFather (then add the bot again).
              </div>
            )}
            {view.groups.map((g) => (
              <div key={g.chat_id} className="flex items-center gap-2">
                <span className="text-zinc-200 truncate">{g.title}</span>
                <span className="text-zinc-500">
                  {g.messages} message{g.messages === 1 ? '' : 's'}{!g.active ? ' · not kept any more' : ''}
                </span>
                <button
                  onClick={() => {
                    if (confirm(`Stop keeping ${g.title} and delete what was kept from it?`)) {
                      run(() => removeInboxGroup(g.chat_id))
                    }
                  }}
                  disabled={busy}
                  title="Remove"
                  className="ml-auto rounded-md p-1 text-zinc-500 hover:bg-zinc-800 hover:text-red-400 disabled:opacity-50"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-zinc-400">
            <span>
              {view.stats.messages} message{view.stats.messages === 1 ? '' : 's'} in {view.stats.chats} chat
              {view.stats.chats === 1 ? '' : 's'}{view.stats.oldest ? ` since ${day(view.stats.oldest)}` : ''}
            </span>
            <span className="flex items-center gap-1.5">Keep for {retention}</span>
            {!view.collecting && (
              <span className="text-amber-600 dark:text-amber-400">Paused — your Telegram link above needs attention</span>
            )}
          </div>

          {view.stats.messages > 0 && (
            <div>
              <button
                onClick={toggleRecent}
                className="flex items-center gap-1 text-xs text-violet-500 hover:underline"
              >
                <ChevronRight className={`h-3 w-3 transition-transform ${recent ? 'rotate-90' : ''}`} />
                What your agents see
              </button>
              {recent && (
                <div className="mt-2 max-h-72 overflow-y-auto rounded-md border border-zinc-800 bg-zinc-950 divide-y divide-zinc-800/60">
                  {recent.map((m) => (
                    <div key={`${m.chat_id}:${m.message_id}`} className="px-3 py-1.5 text-xs">
                      <div className="text-zinc-500">
                        {time(m.at)} · {m.chat} · {m.outgoing ? 'you' : m.sender}
                      </div>
                      <div className="text-zinc-300 whitespace-pre-wrap break-words">
                        {m.deleted ? <span className="italic text-zinc-500">deleted</span> : (
                          <>
                            {m.media && <span className="text-zinc-500">[{m.media}] </span>}
                            {m.text}
                            {m.edited && <span className="text-zinc-500"> (edited)</span>}
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => {
                if (confirm('Delete every message kept so far? The inbox stays on and keeps new ones.')) {
                  run(clearTelegramInbox)
                }
              }}
              disabled={busy || view.stats.messages === 0}
              className="rounded-md bg-zinc-800 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
            >
              Delete kept messages
            </button>
            <button
              onClick={() => {
                if (confirm('Turn the inbox off? Everything kept is deleted, the bot leaves your groups, and your agents can no longer read your Telegram. Also remove the bot where you connected it in Telegram (Settings → Chat Automation, or Telegram Business → Chatbots).')) {
                  run(() => setTelegramInbox({ enabled: false }))
                }
              }}
              disabled={busy}
              className="rounded-md bg-zinc-800 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-700 hover:text-red-400 disabled:opacity-40"
            >
              Turn off
            </button>
          </div>
        </>
      )}

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
          {error}
        </div>
      )}
    </div>
  )
}
