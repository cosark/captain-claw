// Stripe Link wallet (Phase 3): Captain asks Link for a one-time card when it
// buys something; you approve every purchase in Link. US/Canada Link accounts
// only (Stripe's rule), and the deck needs a Link OAuth client from Stripe.
import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Wallet } from 'lucide-react'
import { disconnectLink, getLink, linkConnectUrl, type LinkStatus } from '../../services/payments'

export function LinkPanel() {
  const [s, setS] = useState<LinkStatus | null>(null)
  const [open, setOpen] = useState(false)
  const [msg, setMsg] = useState('')
  const poll = useRef<number | null>(null)
  const refresh = () => getLink().then(setS).catch(() => setS(null))
  useEffect(() => { refresh(); return () => { if (poll.current) clearInterval(poll.current) } }, [])
  if (!s) return null

  const connect = async () => {
    setMsg('')
    try {
      const { url } = await linkConnectUrl()
      window.open(url, '_blank', 'noopener')
      setMsg('Connect your wallet in the Link tab — this panel updates when it\'s done.')
      const started = Date.now()
      poll.current = window.setInterval(async () => {
        const next = await getLink().catch(() => null)
        if (next) setS(next)
        if (next?.connected || Date.now() - started > 180_000) {
          if (poll.current) clearInterval(poll.current)
          poll.current = null
          setMsg(next?.connected ? '' : 'Not connected yet.')
        }
      }, 3000)
    } catch (e) { setMsg(String((e as Error).message || e)) }
  }

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left">
        <span className="flex items-center gap-2 text-sm text-zinc-200">
          <Wallet size={15} className="text-zinc-400" /> Link wallet (shopping)
          <span className="text-xs text-zinc-500">
            {!s.configured ? 'not set up on this deck' : s.connected ? `connected${s.user?.email ? ` · ${s.user.email}` : ''}${s.test_mode ? ' (test)' : ''}` : 'not connected'}
          </span>
        </span>
        {open ? <ChevronDown size={14} className="text-zinc-500" /> : <ChevronRight size={14} className="text-zinc-500" />}
      </button>
      {open && (
        <div className="space-y-2 border-t border-zinc-800 px-3 py-3 text-xs text-zinc-400">
          <p>
            When Captain buys something for you, it asks your Link wallet for a one-time card for that shop and
            amount. You approve each purchase in Link; your real card is never shared, and Captain types the
            one-time card into the shop's form without its AI ever seeing it. Link agent payments are for
            US and Canadian Link accounts.
          </p>
          {!s.configured ? (
            <p className="text-zinc-300">The deck admin needs a Link OAuth client from Stripe (FD_LINK_CLIENT_ID,
              FD_LINK_CLIENT_SECRET, FD_LINK_PUBLISHABLE_KEY), with <code>{'<deck>'}/fd/payments/link/callback</code> as a redirect URI.</p>
          ) : s.connected ? (
            <button onClick={async () => { await disconnectLink(); refresh() }}
              className="rounded-md px-2.5 py-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200">Disconnect</button>
          ) : (
            <button onClick={connect} className="rounded-md bg-sky-700 px-2.5 py-1 font-medium text-white hover:bg-sky-600">Connect Link</button>
          )}
          {s.redirect_uri && <p className="text-[11px] text-zinc-500">Redirect URI: <code>{s.redirect_uri}</code></p>}
          {(msg || s.error) && <p className="text-zinc-300">{msg || s.error}</p>}
        </div>
      )}
    </div>
  )
}
