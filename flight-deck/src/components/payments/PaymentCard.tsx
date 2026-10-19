// One payment an agent prepared — in chat (under the agent's `payments` tool
// call) and on the Payments page. Always the ledger's live state: a card from
// yesterday shows "Paid" once the user tapped "I paid" on their phone.
//
// Captain never moves money: the user scans the barcode in their bank app and
// approves it there. A blocking warning hides the barcode until the owner
// checks it here — the agent can't clear one.
import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Check, CheckCircle2, Copy, Info, Landmark, Loader2, Receipt, X } from 'lucide-react'
import {
  ackWarnings, CLOSED, getPayment, markPayment, PAID, sendToRevolut, statusLabel, useBarcode,
  type Payment, type PaymentWarning,
} from '../../services/payments'

const REVOLUT_STATE: Record<string, string> = {
  CREATED: 'Waiting in Revolut Business — send it from the Revolut app',
  PENDING: 'Sent in Revolut — pending approval or processing',
  COMPLETED: 'Completed in Revolut',
  DECLINED: 'Declined by Revolut', FAILED: 'Failed in Revolut', REVERTED: 'Reverted by Revolut',
  CANCELLED: 'Cancelled in Revolut', DELETED: 'Removed in Revolut',
}

const SOURCE_BADGE: Record<string, { label: string; tone: string }> = {
  barcode: { label: 'from the invoice barcode', tone: 'text-emerald-400' },
  document: { label: 'from the invoice text', tone: 'text-emerald-400' },
  user: { label: 'from you', tone: 'text-emerald-400' },
  model: { label: 'read by AI — please check', tone: 'text-amber-400' },
}

function CopyButton({ value }: { value: string }) {
  const [done, setDone] = useState(false)
  if (!value) return null
  return (
    <button
      type="button"
      title="Copy"
      onClick={() => {
        navigator.clipboard?.writeText(value).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1200)
        })
      }}
      className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300"
    >
      {done ? <Check size={13} /> : <Copy size={13} />}
    </button>
  )
}

function Field({ label, value, copy, source }: { label: string; value: string; copy?: string; source?: string }) {
  if (!value) return null
  const badge = source ? SOURCE_BADGE[source] : undefined
  return (
    <div className="flex items-start justify-between gap-2 border-b border-zinc-800/70 py-1.5 last:border-0">
      <div className="min-w-0">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
        <div className="break-all font-mono text-[13px] text-zinc-200">{value}</div>
        {badge && <div className={`text-[11px] ${badge.tone}`}>{badge.label}</div>}
      </div>
      {copy !== undefined && <CopyButton value={copy} />}
    </div>
  )
}

function WarningRow({ w, onAck, busy }: { w: PaymentWarning; onAck: () => void; busy: boolean }) {
  const blocking = w.severity === 'blocking' && !w.acked
  const tone = w.severity === 'error' ? 'border-red-500/40 bg-red-500/5 text-red-300'
    : blocking ? 'border-amber-500/40 bg-amber-500/5 text-amber-200'
      : 'border-zinc-800 bg-zinc-900/60 text-zinc-400'
  return (
    <div className={`rounded-md border px-2.5 py-2 text-xs ${tone}`}>
      <div className="flex items-start gap-1.5">
        {w.severity === 'info' ? <Info size={13} className="mt-0.5 shrink-0" /> : <AlertTriangle size={13} className="mt-0.5 shrink-0" />}
        <span className="whitespace-pre-wrap">{w.message}</span>
      </div>
      {w.severity === 'blocking' && (w.acked ? (
        <div className="mt-1 pl-5 text-[11px] text-zinc-500">You checked this.</div>
      ) : (
        <button
          disabled={busy}
          onClick={onAck}
          className="ml-5 mt-1.5 rounded bg-amber-600/80 px-2 py-1 text-[11px] font-medium text-white hover:bg-amber-500 disabled:opacity-50"
        >
          I checked — continue
        </button>
      ))}
    </div>
  )
}

function Barcode({ p }: { p: Payment }) {
  const [kind, setKind] = useState<'hub3' | 'epc'>(p.barcodes[0] ?? 'hub3')
  const url = useBarcode(p.id, kind, p.barcodes.length > 0)
  if (!p.barcodes.length) return null
  return (
    <div className="rounded-lg bg-white p-3 text-center">
      {url ? (
        <img src={url} alt="Payment barcode" className={`mx-auto ${kind === 'epc' ? 'w-44' : 'w-full max-w-[420px]'}`} style={{ imageRendering: 'pixelated' }} />
      ) : (
        <Loader2 className="mx-auto my-6 h-5 w-5 animate-spin text-zinc-400" />
      )}
      <div className="mt-1.5 text-[11px] text-zinc-600">
        {kind === 'hub3' ? 'Scan in your bank app: Pay → scan barcode (HUB3)' : 'QR code for SEPA bank apps'}
      </div>
      {p.barcodes.length > 1 && (
        <div className="mt-1.5 flex justify-center gap-1">
          {p.barcodes.map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={`rounded px-2 py-0.5 text-[11px] ${k === kind ? 'bg-zinc-800 text-white' : 'bg-zinc-200 text-zinc-700'}`}
            >
              {k === 'hub3' ? 'HUB3' : 'EPC QR'}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export function PaymentCard({ id, compact = false }: { id: string; compact?: boolean }) {
  const [p, setP] = useState<Payment | null>(null)
  const [rails, setRails] = useState<string[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    getPayment(id).then((r) => { setP(r.intent); setRails(r.rails || []); setError('') })
      .catch((e) => setError(String(e.message || e)))
  }, [id])
  useEffect(() => { load() }, [load])

  const act = async (fn: () => Promise<{ intent: Payment }>) => {
    setBusy(true)
    try {
      setP((await fn()).intent)
      setError('')
    } catch (e) {
      setError(String((e as Error).message || e))
      load()   // e.g. Verification of Payee just added a warning
    } finally {
      setBusy(false)
    }
  }

  if (error && !p) {
    // Not this user's payment, or payments are off: say nothing in chat.
    return compact ? null : <div className="text-xs text-zinc-500">{error}</div>
  }
  if (!p) {
    return (
      <div className="mb-3 flex items-center gap-2 rounded-lg border border-zinc-800 px-3 py-2 text-xs text-zinc-500">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading payment…
      </div>
    )
  }

  const paid = PAID.includes(p.status)
  const draft = p.revolut?.draft_id ? p.revolut : null
  const inRevolut = !!draft && p.status === 'handed_off'
  const closed = CLOSED.includes(p.status)
  const payable = p.status === 'ready' || p.status === 'handed_off'
  const openBlocking = p.warnings.filter((w) => w.severity === 'blocking' && !w.acked)
  const ref = [p.model, p.reference].filter(Boolean).join(' ')
  const euros = `${Math.floor(p.amount_cents / 100)},${String(p.amount_cents % 100).padStart(2, '0')}`

  return (
    <div className="mb-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-zinc-500">
            <Receipt size={12} /> Payment · {statusLabel(p.status)}
          </div>
          <div className="truncate text-sm font-semibold text-zinc-100">{p.payee_name || '—'}</div>
          {p.bank && <div className="text-[11px] text-zinc-500">{p.bank}</div>}
        </div>
        <div className="text-right">
          <div className={`text-xl font-bold ${paid ? 'text-emerald-400' : 'text-zinc-100'}`}>{p.amount_text}</div>
          {p.due_date && <div className="text-[11px] text-zinc-500">due {p.due_date}</div>}
        </div>
      </div>

      {paid && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-emerald-400">
          <CheckCircle2 size={14} />
          {p.status === 'settled' ? 'Paid — confirmed by the bank' : 'Marked as paid'}
          {p.paid_at ? ` on ${new Date(p.paid_at * 1000).toLocaleDateString()}` : ''}
        </div>
      )}

      {draft && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-sky-300">
          <Landmark size={14} />
          {REVOLUT_STATE[draft.state || 'CREATED'] || draft.state}
          {draft.reason ? ` (${draft.reason})` : ''}
        </div>
      )}

      {!closed && p.warnings.length > 0 && (
        <div className="mt-2 space-y-1.5">
          {p.warnings.map((w) => (
            <WarningRow key={w.code} w={w} busy={busy} onAck={() => act(() => ackWarnings(p.id, [w.code]))} />
          ))}
        </div>
      )}

      {!closed && (
        <div className={`mt-2 grid gap-3 ${compact ? '' : 'md:grid-cols-2'}`}>
          <div>
            <Field label="IBAN" value={p.iban_grouped} copy={p.iban} source={p.provenance.iban} />
            <Field label="Amount" value={p.amount_text} copy={euros} source={p.provenance.amount_cents} />
            <Field label="Model · poziv na broj" value={ref} copy={p.reference} source={p.provenance.reference} />
            <Field label="Description" value={p.description} />
          </div>
          {payable && openBlocking.length === 0 && !inRevolut && <Barcode p={p} />}
        </div>
      )}

      {payable && (
        <div className="mt-3 flex flex-wrap gap-2">
          {rails.includes('revolut') && !inRevolut && openBlocking.length === 0 && (
            <button
              disabled={busy}
              onClick={() => act(() => sendToRevolut(p.id))}
              title="Creates a draft in Revolut Business — you send it from the Revolut app"
              className="inline-flex items-center gap-1 rounded-md bg-sky-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-600 disabled:opacity-50"
            >
              <Landmark size={13} /> Send to Revolut
            </button>
          )}
          {!inRevolut && (
            <button
              disabled={busy}
              onClick={() => act(() => markPayment(p.id, 'user_paid'))}
              className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
            >
              <Check size={13} /> I paid
            </button>
          )}
          <button
            disabled={busy}
            onClick={() => act(() => markPayment(p.id, 'skipped'))}
            className="inline-flex items-center gap-1 rounded-md bg-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50"
          >
            <X size={13} /> Skip
          </button>
        </div>
      )}
      {error && p && <div className="mt-2 text-[11px] text-red-400">{error}</div>}
      {!closed && (
        <div className="mt-2 text-[10px] text-zinc-600">
          Captain never moves money — you approve the payment in your bank app.
        </div>
      )}
    </div>
  )
}
