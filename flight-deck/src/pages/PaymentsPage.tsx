// Payments — every bill an agent got ready, what's still to pay, and the
// owner's limit. Opened from the sidebar, a bell notification, or a phone page
// link (/payments/<id>) when a payment needs review.
import { useCallback, useEffect, useState } from 'react'
import { Loader2, Receipt } from 'lucide-react'
import { PaymentCard } from '../components/payments/PaymentCard'
import { RevolutPanel } from '../components/payments/RevolutPanel'
import { BankLinkPanel } from '../components/payments/BankLinkPanel'
import { LinkPanel } from '../components/payments/LinkPanel'
import { ProtocolsPanel } from '../components/payments/ProtocolsPanel'
import { PurchaseCard } from '../components/payments/PurchaseCard'
import {
  CLOSED, listPayments, listPurchases, PURCHASE_LABEL, setPaymentsCap, statusLabel,
  type Payment, type PaymentsConfig, type Purchase,
} from '../services/payments'

const FILTERS: { id: string; label: string; status: string }[] = [
  { id: 'open', label: 'To pay', status: 'draft,needs_review,ready,handed_off' },
  { id: 'paid', label: 'Paid', status: 'user_paid,settled' },
  { id: 'all', label: 'All', status: '' },
]

/** /payments/<id> opens that payment. */
export function paymentIdFromPath(path = window.location.pathname): string {
  const m = /^\/payments\/(pay_[0-9a-f]{12})\/?$/.exec(path)
  return m ? m[1] : ''
}

/** /payments/purchases/<id> opens that purchase. */
export function purchaseIdFromPath(path = window.location.pathname): string {
  const m = /^\/payments\/purchases\/(pur_[0-9a-f]{12})\/?$/.exec(path)
  return m ? m[1] : ''
}

function Purchases() {
  const [rows, setRows] = useState<Purchase[] | null>(null)
  const [selected, setSelected] = useState(purchaseIdFromPath())
  useEffect(() => { listPurchases().then((r) => setRows(r.purchases)).catch(() => setRows([])) }, [])
  if (!rows || (rows.length === 0 && !selected)) return null
  return (
    <div className="mb-6">
      <h2 className="mb-2 text-sm font-semibold text-zinc-200">Purchases</h2>
      {selected && <PurchaseCard key={selected} id={selected}
        onChange={(np) => setRows((rs) => rs && rs.map((r) => (r.id === np.id ? np : r)))} />}
      <div className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
        {rows.map((p) => (
          <button key={p.id} onClick={() => setSelected(p.id === selected ? '' : p.id)}
            className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-zinc-900 ${p.id === selected ? 'bg-zinc-900' : ''}`}>
            <div className="min-w-0">
              <div className="truncate text-sm text-zinc-200">{p.merchant_name} · <span className="text-zinc-500">{p.merchant_domain}</span></div>
              <div className="text-[11px] text-zinc-500">{PURCHASE_LABEL[p.status] ?? p.status}{p.test ? ' · test' : ''}</div>
            </div>
            <div className="shrink-0 text-sm font-medium text-zinc-300">{p.amount_text}</div>
          </button>
        ))}
      </div>
    </div>
  )
}

function CapEditor({ config, onSaved }: { config: PaymentsConfig; onSaved: (c: PaymentsConfig) => void }) {
  const [value, setValue] = useState(String(config.cap_cents / 100))
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const save = async () => {
    const euros = Number(value.replace(',', '.'))
    if (!Number.isFinite(euros) || euros < 0) { setMsg('Enter an amount in EUR (0 = no limit).'); return }
    setSaving(true)
    try {
      onSaved(await setPaymentsCap(Math.round(euros * 100)))
      setMsg('Saved — applies to payments prepared from now on.')
    } catch (e) {
      setMsg(String((e as Error).message || e))
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-400">
      <span>Warn above</span>
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="w-28 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
        inputMode="decimal"
      />
      <span>EUR</span>
      <button
        disabled={saving}
        onClick={save}
        className="rounded-md bg-zinc-700 px-2.5 py-1 font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50"
      >
        Save limit
      </button>
      {msg && <span className="text-zinc-500">{msg}</span>}
    </div>
  )
}

export function PaymentsPage() {
  const [filter, setFilter] = useState('open')
  const [rows, setRows] = useState<Payment[] | null>(null)
  const [config, setConfig] = useState<PaymentsConfig | null>(null)
  const [selected, setSelected] = useState(paymentIdFromPath())
  const [error, setError] = useState('')

  const load = useCallback(() => {
    const status = FILTERS.find((f) => f.id === filter)?.status ?? ''
    listPayments(status)
      .then(({ payments, ...cfg }) => { setRows(payments); setConfig(cfg); setError('') })
      .catch((e) => setError(String(e.message || e)))
  }, [filter])
  useEffect(() => { load() }, [load])

  if (error) {
    return (
      <div className="p-6 text-sm text-zinc-400">
        {error.includes('turned off') ? 'Payments are turned off on this deck (FD_PAYMENTS).' : error}
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-4xl">
        <div className="mb-1 flex items-center gap-2">
          <Receipt className="h-5 w-5 text-zinc-400" />
          <h1 className="text-lg font-semibold text-zinc-100">Payments</h1>
        </div>
        <p className="mb-4 text-sm text-zinc-500">
          Bills your agents got ready. Captain never moves money: scan a payment's barcode in your
          bank app and approve it there, then mark it paid.
        </p>
        {config && !config.deps_ok && (
          <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-xs text-amber-200">
            This deck is missing the payments libraries (pip install .[payments]) — barcodes can't be made.
          </div>
        )}
        {config && <div className="mb-4"><CapEditor config={config} onSaved={setConfig} /></div>}
        <BankLinkPanel />
        <RevolutPanel />
        <LinkPanel />
        <ProtocolsPanel />
        <Purchases />

        <div className="mb-3 flex gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={`rounded-md px-3 py-1 text-xs ${f.id === filter ? 'bg-zinc-700 text-white' : 'text-zinc-400 hover:bg-zinc-800'}`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {selected && (
          <div className="mb-4">
            <PaymentCard key={selected} id={selected} />
          </div>
        )}

        {rows === null ? (
          <Loader2 className="h-5 w-5 animate-spin text-zinc-500" />
        ) : rows.length === 0 ? (
          <p className="text-sm text-zinc-500">Nothing here.</p>
        ) : (
          <div className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
            {rows.map((p) => (
              <button
                key={p.id}
                onClick={() => setSelected(p.id === selected ? '' : p.id)}
                className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-zinc-900 ${p.id === selected ? 'bg-zinc-900' : ''}`}
              >
                <div className="min-w-0">
                  <div className="truncate text-sm text-zinc-200">{p.payee_name || '—'}</div>
                  <div className="text-[11px] text-zinc-500">
                    {statusLabel(p.status)}
                    {p.due_date ? ` · due ${p.due_date}` : ''}
                    {p.warnings.some((w) => w.severity === 'blocking' && !w.acked) && !CLOSED.includes(p.status)
                      ? ' · needs your review' : ''}
                  </div>
                </div>
                <div className="shrink-0 text-sm font-medium text-zinc-300">{p.amount_text}</div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
