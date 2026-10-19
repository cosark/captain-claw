// Due-date reminders (Phase 4c): one daily digest of the bills due soon, today
// or overdue — in the bell and in the chat your bills came from. Flight Deck
// writes it; no model does. Switch it off here, or per bill on its card.
import { useEffect, useState } from 'react'
import { BellRing, ChevronDown, ChevronRight } from 'lucide-react'
import { getReminders, setReminders, type ReminderSettings } from '../../services/payments'

const SELECT = 'rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100'

export function RemindersPanel() {
  const [s, setS] = useState<ReminderSettings | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  useEffect(() => { getReminders().then(setS).catch(() => setS(null)) }, [])
  if (!s) return null

  const save = async (patch: Partial<ReminderSettings>) => {
    const next = { ...s, ...patch }
    setBusy(true); setMsg('')
    try {
      setS(await setReminders({ enabled: next.enabled, days_before: next.days_before, time: next.time, overdue: next.overdue }))
    } catch (e) { setMsg(String((e as Error).message || e)) } finally { setBusy(false) }
  }
  const where = s.chat ? `the bell and ${s.chat.label}` : 'the bell'

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left">
        <span className="flex min-w-0 items-center gap-2 text-sm text-zinc-200">
          <BellRing size={15} className="shrink-0 text-zinc-400" /> Reminders
          <span className="truncate text-xs text-zinc-500">{s.enabled ? `daily at ${s.time} · ${where}` : 'off'}</span>
        </span>
        {open ? <ChevronDown size={14} className="text-zinc-500" /> : <ChevronRight size={14} className="text-zinc-500" />}
      </button>
      {open && (
        <div className="space-y-3 border-t border-zinc-800 px-3 py-3 text-xs text-zinc-400">
          <p>
            Once a day Captain sends one message listing the bills due soon, due today or overdue, each with a link
            to its barcode and "I paid". It goes to {where}
            {s.chat ? ' — the chat you last prepared a bill from.' : '. Prepare a bill from WhatsApp or Telegram and the reminders go there too.'}
            {' '}Paid bills drop out as soon as you mark them paid or your bank statement shows them.
          </p>
          <label className="flex items-center gap-2 text-zinc-300">
            <input type="checkbox" disabled={busy} checked={s.enabled} onChange={(e) => save({ enabled: e.target.checked })} />
            Remind me about due bills
          </label>
          {s.enabled && (
            <div className="flex flex-wrap items-center gap-2 text-zinc-300">
              <span>First reminder</span>
              <select disabled={busy} value={s.days_before} onChange={(e) => save({ days_before: Number(e.target.value) })} className={SELECT}>
                {[0, 1, 2, 3, 5, 7, 10, 14].map((d) => (
                  <option key={d} value={d}>{d === 0 ? 'on the due date' : `${d} day${d === 1 ? '' : 's'} before`}</option>
                ))}
              </select>
              <span>at</span>
              <input type="time" disabled={busy} value={s.time} onChange={(e) => e.target.value && save({ time: e.target.value })}
                className={SELECT} />
              <label className="flex items-center gap-2">
                <input type="checkbox" disabled={busy} checked={s.overdue} onChange={(e) => save({ overdue: e.target.checked })} />
                and weekly while overdue
              </label>
            </div>
          )}
          {s.quiet_hours && <p className="text-[11px] text-zinc-500">Quiet hours on this deck: {s.quiet_hours} h — nothing is sent then.</p>}
          {msg && <p className="text-zinc-300">{msg}</p>}
        </div>
      )}
    </div>
  )
}
