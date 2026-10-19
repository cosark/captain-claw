// One purchase Captain wants to make (Phase 3). Nothing can be paid until you
// approve this seller and amount — in Link for Link purchases, here for a
// Revolut card made for the purchase. The card number never reaches the agent's
// model: Captain types it into the shop's form and pays in one step.
import { useCallback, useEffect, useState } from 'react'
import { Check, CheckCircle2, ExternalLink, Loader2, ShoppingBag, X } from 'lucide-react'
import {
  approvePurchase, denyPurchase, getPurchase, PURCHASE_LABEL, type Purchase,
} from '../../services/payments'

const SOURCE: Record<Purchase['source'], string> = {
  link: 'one-time Link card', revolut_card: 'a Revolut card made for this purchase',
  acp: "the shop's ACP checkout", ap2: 'an AP2 checkout',
}

export function PurchaseCard({ id, compact = false }: { id: string; compact?: boolean }) {
  const [p, setP] = useState<Purchase | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => {
    getPurchase(id).then((r) => { setP(r.purchase); setError('') }).catch((e) => setError(String(e.message || e)))
  }, [id])
  useEffect(() => { load() }, [load])
  // While it waits on Link (or a bank confirmation), look again now and then.
  useEffect(() => {
    if (!p || !['awaiting_approval', 'paying', 'needs_action', 'ordered'].includes(p.status)) return
    const t = window.setInterval(load, 8000)
    return () => window.clearInterval(t)
  }, [p, load])

  const act = async (fn: () => Promise<{ purchase: Purchase }>) => {
    setBusy(true)
    try { setP((await fn()).purchase); setError('') } catch (e) { setError(String((e as Error).message || e)); load() } finally { setBusy(false) }
  }

  if (error && !p) return compact ? null : <div className="text-xs text-zinc-500">{error}</div>
  if (!p) {
    return (
      <div className="mb-3 flex items-center gap-2 rounded-lg border border-zinc-800 px-3 py-2 text-xs text-zinc-500">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading purchase…
      </div>
    )
  }
  const done = p.status === 'ordered' || p.status === 'completed'
  const waiting = p.status === 'awaiting_approval'

  return (
    <div className="mb-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-zinc-500">
            <ShoppingBag size={12} /> Purchase · {PURCHASE_LABEL[p.status] ?? p.status}{p.test ? ' · test' : ''}
          </div>
          <div className="truncate text-sm font-semibold text-zinc-100">{p.merchant_name}</div>
          <a href={p.merchant_url} target="_blank" rel="noreferrer" className="text-[11px] text-zinc-500 hover:text-zinc-300">
            {p.merchant_domain}
          </a>
        </div>
        <div className="text-right">
          <div className={`text-xl font-bold ${done ? 'text-emerald-400' : 'text-zinc-100'}`}>{p.amount_text}</div>
          {p.card_last4 && <div className="text-[11px] text-zinc-500">card ••{p.card_last4}</div>}
        </div>
      </div>

      {p.items.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-zinc-300">
          {p.items.map((it, i) => (
            <li key={i}>{it.quantity && it.quantity > 1 ? `${it.quantity} × ` : ''}{it.name}{it.unit_amount ? ` · ${it.unit_amount}` : ''}</li>
          ))}
        </ul>
      )}
      <div className="mt-1 text-[11px] text-zinc-500">Paid with {SOURCE[p.source]}.</div>
      {p.warnings.map((w) => <div key={w.code} className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-2 py-1 text-xs text-amber-200">{w.message}</div>)}

      {done && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-emerald-400">
          <CheckCircle2 size={14} /> {p.status === 'completed' ? 'Ordered and paid' : 'Ordered'}{p.order_ref ? ` — order ${p.order_ref}` : ''}
        </div>
      )}
      {p.status === 'needs_action' && (
        <div className="mt-2 text-xs text-sky-300">
          {p.next_action.display_message || 'Your bank wants you to confirm the payment in its app.'}
          {p.next_action.action_url && <a href={p.next_action.action_url} target="_blank" rel="noreferrer" className="ml-1 underline">Open</a>}
        </div>
      )}
      {p.result?.excerpt && !done && p.status === 'failed' && (
        <div className="mt-2 text-[11px] text-zinc-500">The shop said: {p.result.excerpt.slice(0, 200)}</div>
      )}

      {waiting && (
        <div className="mt-3 flex flex-wrap gap-2">
          {p.source === 'link' ? (
            <a href={p.approval_url} target="_blank" rel="noreferrer"
              className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500">
              Approve in Link <ExternalLink size={12} />
            </a>
          ) : (
            <button disabled={busy} onClick={() => act(() => approvePurchase(p.id))}
              className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50">
              <Check size={13} /> Approve {p.amount_text} at {p.merchant_domain}
            </button>
          )}
          <button disabled={busy} onClick={() => act(() => denyPurchase(p.id))}
            className="inline-flex items-center gap-1 rounded-md bg-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50">
            <X size={13} /> Deny
          </button>
        </div>
      )}
      {error && p && <div className="mt-2 text-[11px] text-red-400">{error}</div>}
    </div>
  )
}
