// Checkout protocols (Phase 3): shops Captain can buy from without a browser.
// ACP — the shop gives you an agent key; Captain checks out with the shop and
// you approve the payment in Link. AP2 — your credentials provider holds the
// card; you approve here and Flight Deck signs the mandates with your key.
import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Plug, Trash2 } from 'lucide-react'
import {
  addAcpMerchant, getAp2Settings, listAcpMerchants, removeAcpMerchant, setAp2Settings,
  type AcpMerchant, type Ap2Settings,
} from '../../services/payments'
import { CopyBlock } from './RevolutPanel'

const INPUT = 'min-w-0 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100'
const BUTTON = 'rounded-md bg-zinc-700 px-2.5 py-1 font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50'

export function ProtocolsPanel() {
  const [open, setOpen] = useState(false)
  const [shops, setShops] = useState<AcpMerchant[] | null>(null)
  const [ap2, setAp2] = useState<Ap2Settings | null>(null)
  const [shop, setShop] = useState({ domain: '', api_key: '', name: '' })
  const [cp, setCp] = useState({ url: '', email: '' })
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    listAcpMerchants().then((r) => setShops(r.merchants)).catch(() => setShops(null))
    getAp2Settings().then((s) => { setAp2(s); setCp({ url: s.credentials_provider_url, email: s.user_email }) })
      .catch(() => setAp2(null))
  }, [])
  if (!shops && !ap2) return null

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setMsg('')
    try { await fn() } catch (e) { setMsg(String((e as Error).message || e)) } finally { setBusy(false) }
  }
  const summary = [shops?.length ? `${shops.length} ACP shop${shops.length === 1 ? '' : 's'}` : '',
    ap2?.enabled && ap2.credentials_provider_url ? 'AP2 set up' : ''].filter(Boolean).join(' · ') || 'none set up'

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left">
        <span className="flex items-center gap-2 text-sm text-zinc-200">
          <Plug size={15} className="text-zinc-400" /> Checkout protocols (ACP · AP2)
          <span className="text-xs text-zinc-500">{summary}</span>
        </span>
        {open ? <ChevronDown size={14} className="text-zinc-500" /> : <ChevronRight size={14} className="text-zinc-500" />}
      </button>
      {open && (
        <div className="space-y-4 border-t border-zinc-800 px-3 py-3 text-xs text-zinc-400">
          <section className="space-y-2">
            <div className="font-medium text-zinc-200">ACP shops</div>
            <p>
              Shops that support the Agentic Commerce Protocol give you an agent key. Captain then orders from
              the shop directly — no browser, no card form — and you approve the payment in your Link wallet.
            </p>
            {shops && shops.length > 0 && (
              <ul className="space-y-1">
                {shops.map((m) => (
                  <li key={m.domain} className="flex items-center justify-between gap-2 rounded-md border border-zinc-800 px-2 py-1">
                    <span className="truncate text-zinc-200">{m.name || m.domain}{m.name && <span className="ml-1 text-zinc-500">{m.domain}</span>}</span>
                    <button disabled={busy} title="Remove" onClick={() => run(async () => setShops((await removeAcpMerchant(m.domain)).merchants))}
                      className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"><Trash2 size={13} /></button>
                  </li>
                ))}
              </ul>
            )}
            <form className="flex flex-wrap gap-2" onSubmit={(e) => {
              e.preventDefault()
              run(async () => {
                setShops((await addAcpMerchant(shop.domain, shop.api_key, shop.name)).merchants)
                setShop({ domain: '', api_key: '', name: '' })
              })
            }}>
              <input value={shop.domain} onChange={(e) => setShop({ ...shop, domain: e.target.value })} placeholder="shop.example"
                className={`${INPUT} w-40`} />
              <input value={shop.api_key} onChange={(e) => setShop({ ...shop, api_key: e.target.value })} placeholder="ACP key"
                type="password" autoComplete="off" className={`${INPUT} w-48 font-mono`} />
              <input value={shop.name} onChange={(e) => setShop({ ...shop, name: e.target.value })} placeholder="Name (optional)"
                className={`${INPUT} w-36`} />
              <button type="submit" disabled={busy || !shop.domain.trim() || !shop.api_key.trim()} className={BUTTON}>Add shop</button>
            </form>
          </section>

          <section className="space-y-2">
            <div className="font-medium text-zinc-200">AP2</div>
            {!ap2?.enabled ? (
              <p>Not on for this deck — the deck admin turns it on with <code>FD_AP2=1</code>.</p>
            ) : (
              <>
                <p>
                  With AP2, your credentials provider (a wallet that speaks AP2) holds your card. When Captain finds
                  what to buy, you approve it here; Flight Deck signs the checkout and payment mandates with your
                  own key, and any one-time code the shop asks for comes back to you.
                </p>
                <form className="flex flex-wrap gap-2" onSubmit={(e) => {
                  e.preventDefault()
                  run(async () => setAp2(await setAp2Settings(cp.url, cp.email)))
                }}>
                  <input value={cp.url} onChange={(e) => setCp({ ...cp, url: e.target.value })} placeholder="Credentials provider A2A URL"
                    className={`${INPUT} w-72`} />
                  <input value={cp.email} onChange={(e) => setCp({ ...cp, email: e.target.value })} placeholder="Wallet email"
                    type="email" className={`${INPUT} w-48`} />
                  <button type="submit" disabled={busy || !cp.url.trim() || !cp.email.trim()} className={BUTTON}>Save</button>
                </form>
                {ap2.public_key && (
                  <>
                    <CopyBlock label={`Your mandate signing key (public, kid ${ap2.kid}) — give it to your credentials provider`}
                      value={ap2.public_key} />
                    <p className="text-[11px] text-zinc-500">Shops see Captain as shopping agent <code>{ap2.shopping_agent_id}</code>.</p>
                  </>
                )}
              </>
            )}
          </section>
          {msg && <p className="text-zinc-300">{msg}</p>}
        </div>
      )}
    </div>
  )
}
