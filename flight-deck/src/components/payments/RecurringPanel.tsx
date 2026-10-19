// Recurring bills (Phase 4d): bills Captain saw repeat — in your paid bills or
// on your bank statement. Nothing happens until you track one. Then Captain
// prepares the fixed ones before they're due (you still pay them in your bank),
// watches for the invoices of the others, and notices when the bank paid one
// directly (a direct debit or standing order).
import { useCallback, useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Repeat, Trash2 } from 'lucide-react'
import { deleteRecurring, listRecurring, updateRecurring, type RecurringBill } from '../../services/payments'

const CADENCE: Record<RecurringBill['cadence'], string> = {
  monthly: 'monthly', bimonthly: 'every two months', quarterly: 'quarterly', yearly: 'yearly',
}
const PERIOD: Record<string, { label: string; tone: string }> = {
  paid: { label: 'paid', tone: 'bg-emerald-500/15 text-emerald-300' },
  covered: { label: 'paid by the bank', tone: 'bg-emerald-500/15 text-emerald-300' },
  prepared: { label: 'bill ready', tone: 'bg-sky-500/15 text-sky-300' },
  expected: { label: 'expected', tone: 'bg-zinc-700/50 text-zinc-300' },
  missing: { label: 'not come yet', tone: 'bg-amber-500/15 text-amber-300' },
  skipped: { label: 'skipped', tone: 'bg-zinc-700/50 text-zinc-400' },
}
const SELECT = 'rounded-md border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 text-zinc-100'

const day = (iso: string) => {
  const d = new Date(`${iso}T12:00:00`)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString()
}

function when(b: RecurringBill): string {
  return b.cadence === 'yearly' ? 'yearly' : `${CADENCE[b.cadence]}, around the ${b.anchor_day}.`
}

export function RecurringPanel() {
  const [bills, setBills] = useState<RecurringBill[] | null>(null)
  const [revolut, setRevolut] = useState(false)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const load = useCallback(() => listRecurring().then((r) => { setBills(r.bills); setRevolut(r.revolut) })
    .catch(() => setBills(null)), [])
  useEffect(() => { load() }, [load])
  if (!bills) return null

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setMsg('')
    try { await fn() } catch (e) { setMsg(String((e as Error).message || e)) } finally { setBusy(false); load() }
  }
  const suggested = bills.filter((b) => b.status === 'suggested')
  const tracked = bills.filter((b) => b.status === 'active' || b.status === 'paused')
  const summary = [tracked.length ? `${tracked.length} tracked` : '', suggested.length ? `${suggested.length} to confirm` : '']
    .filter(Boolean).join(' · ') || 'none yet'

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left">
        <span className="flex min-w-0 items-center gap-2 text-sm text-zinc-200">
          <Repeat size={15} className="shrink-0 text-zinc-400" /> Recurring bills
          <span className={`truncate text-xs ${suggested.length ? 'text-sky-300' : 'text-zinc-500'}`}>{summary}</span>
        </span>
        {open ? <ChevronDown size={14} className="text-zinc-500" /> : <ChevronRight size={14} className="text-zinc-500" />}
      </button>
      {open && (
        <div className="space-y-3 border-t border-zinc-800 px-3 py-3 text-xs text-zinc-400">
          <p>
            Bills Captain saw repeat. Track one and Captain prepares it before it's due when nothing changes (rent,
            kindergarten) — you still pay it in your bank — or watches for its invoice when it does (electricity,
            water), and tells you when one hasn't come. A direct debit on your statement counts as paid.
          </p>
          {suggested.map((b) => (
            <div key={b.id} className="rounded-md border border-sky-500/40 bg-sky-500/5 px-2.5 py-2">
              <div className="text-zinc-200">
                Track <b>{b.payee_name || b.iban_grouped}</b> as a {CADENCE[b.cadence]} bill?
                <span className="text-zinc-400"> {when(b)} · {b.amount_text}</span>
              </div>
              <div className="text-[11px] text-zinc-500">
                {b.source === 'statement' ? 'Seen on your bank statement' : b.source === 'user' ? 'You said it repeats'
                  : 'Seen in your paid bills'}{b.occurrences > 1 ? ` · ${b.occurrences} times` : ''}
              </div>
              <div className="mt-1.5 flex gap-2">
                <button disabled={busy} onClick={() => run(() => updateRecurring(b.id, { status: 'active' }))}
                  className="rounded bg-emerald-600 px-2 py-0.5 font-medium text-white hover:bg-emerald-500 disabled:opacity-50">Track it</button>
                <button disabled={busy} onClick={() => run(() => updateRecurring(b.id, { status: 'dismissed' }))}
                  className="rounded bg-zinc-700 px-2 py-0.5 font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50">Dismiss</button>
              </div>
            </div>
          ))}
          {tracked.map((b) => (
            <div key={b.id} className="space-y-1.5 rounded-md border border-zinc-800 px-2.5 py-2">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="font-medium text-zinc-200">{b.payee_name || b.iban_grouped}</span>
                <span>{b.amount_text}</span>
                <select disabled={busy} value={b.cadence} className={SELECT}
                  onChange={(e) => run(() => updateRecurring(b.id, { cadence: e.target.value as RecurringBill['cadence'] }))}>
                  {Object.entries(CADENCE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </select>
                {b.cadence !== 'yearly' && <span>around the {b.anchor_day}.</span>}
                {b.status === 'paused' && <span className="text-amber-300">paused</span>}
                <span className="ml-auto flex gap-1">
                  <button disabled={busy} onClick={() => run(() => updateRecurring(b.id, { status: b.status === 'paused' ? 'active' : 'paused' }))}
                    className="rounded px-2 py-0.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200">
                    {b.status === 'paused' ? 'Resume' : 'Pause'}
                  </button>
                  <button disabled={busy} title="Stop tracking" onClick={() => run(() => deleteRecurring(b.id))}
                    className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"><Trash2 size={13} /></button>
                </span>
              </div>
              {b.next_due && b.status === 'active' && <div className="text-[11px] text-zinc-500">Next around {day(b.next_due)}</div>}
              {b.amount_kind === 'fixed' && (
                <label className="flex items-center gap-2">
                  <input type="checkbox" disabled={busy} checked={b.reference_kind === 'fixed'}
                    onChange={(e) => run(() => updateRecurring(b.id, { reference_kind: e.target.checked ? 'fixed' : 'per_bill' }))} />
                  Same amount and reference every time{b.reference ? ` (${b.reference})` : ''}
                </label>
              )}
              {b.can_prepare && (
                <label className="flex items-center gap-2">
                  <input type="checkbox" disabled={busy} checked={b.auto_prepare}
                    onChange={(e) => run(() => updateRecurring(b.id, { auto_prepare: e.target.checked }))} />
                  Prepare it for me 5 days before it's due
                </label>
              )}
              {b.can_prepare && b.auto_prepare && revolut && (
                <label className="flex items-center gap-2">
                  <input type="checkbox" disabled={busy} checked={b.revolut_draft}
                    onChange={(e) => run(() => updateRecurring(b.id, { revolut_draft: e.target.checked }))} />
                  Also put it in Revolut Business as a draft scheduled for the due date (you approve it there)
                </label>
              )}
              {b.periods.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {b.periods.map((p) => {
                    const look = PERIOD[p.status] ?? PERIOD.expected
                    return (
                      <span key={p.expected} title={p.note || undefined} className={`rounded px-1.5 py-0.5 text-[10px] ${look.tone}`}>
                        {day(p.expected)} · {look.label}
                      </span>
                    )
                  })}
                </div>
              )}
            </div>
          ))}
          {bills.length === 0 && <p className="text-zinc-500">Nothing yet — Captain suggests bills here once it has seen them repeat.</p>}
          {msg && <p className="text-zinc-300">{msg}</p>}
        </div>
      )}
    </div>
  )
}
