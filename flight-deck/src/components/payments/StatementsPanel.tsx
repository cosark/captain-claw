// Bank statements (Phase 4): read-only access to your own account, so Captain
// can confirm from the bank that a bill was paid. Only outgoing payments are
// kept; incoming ones and balances never are, and nothing here can move money.
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, FileSearch, Loader2, RefreshCw } from 'lucide-react'
import {
  connectStatements, disconnectStatements, getBankLink, getStatements, setRevolutStatement, statementBanks,
  syncStatements, watchStatementAccount, type StatementBank, type StatementSession, type StatementsStatus,
} from '../../services/payments'

const COUNTRIES = ['HR', 'SI', 'AT', 'DE', 'IT', 'LT', 'FI', 'FR', 'ES', 'NL', 'BE', 'PL', 'SK', 'CZ']
const SELECT = 'rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100'
const BUTTON = 'rounded-md bg-zinc-700 px-2.5 py-1 font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50'

function ago(ts: number | null): string {
  if (!ts) return 'never'
  const min = Math.round((Date.now() / 1000 - ts) / 60)
  if (min < 2) return 'just now'
  if (min < 60) return `${min} min ago`
  const h = Math.round(min / 60)
  return h < 36 ? `${h} h ago` : new Date(ts * 1000).toLocaleDateString()
}

const day = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleDateString() : '')

export function StatementsPanel() {
  const [s, setS] = useState<StatementsStatus | null>(null)
  const [open, setOpen] = useState(false)
  const [country, setCountry] = useState('HR')
  const [psuType, setPsuType] = useState('personal')
  const [banks, setBanks] = useState<StatementBank[] | null>(null)
  const [bank, setBank] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const poll = useRef<number | null>(null)

  const refresh = useCallback(() => getStatements().then(setS).catch(() => setS(null)), [])
  useEffect(() => {
    refresh()
    // Start from the bank the pay-by-bank link already uses.
    getBankLink().then((b) => {
      if (b.bank) { setCountry(b.bank.country); setPsuType(b.bank.psu_type); setBank(b.bank.name) }
    }).catch(() => {})
    return () => { if (poll.current) window.clearInterval(poll.current) }
  }, [refresh])
  useEffect(() => {
    if (!open || !s?.enabled || !s.configured) return
    setBanks(null)
    statementBanks(country, psuType).then((r) => setBanks(r.banks)).catch((e) => { setBanks([]); setMsg(String(e.message || e)) })
  }, [open, s?.enabled, s?.configured, country, psuType])

  if (!s) return null

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setMsg('')
    try { await fn() } catch (e) { setMsg(String((e as Error).message || e)) } finally { setBusy(false); refresh() }
  }
  const connect = (name: string) => {
    // Open the tab on the click itself (a popup opened after the network call can be blocked).
    const tab = window.open('', '_blank')
    if (tab) tab.opener = null
    run(async () => {
      try {
        const { url } = await connectStatements(name, country, psuType)
        if (tab) tab.location.href = url
        else window.location.href = url
      } catch (e) { tab?.close(); throw e }
      setMsg('Approve read access in your bank\'s tab — this panel updates when it\'s done.')
      const started = Date.now()
      if (poll.current) window.clearInterval(poll.current)
      poll.current = window.setInterval(async () => {
        const next = await getStatements().catch(() => null)
        const connected = !!next?.sessions.some((x) => x.provider === 'enablebanking' && (x.last_sync_at ?? 0) * 1000 > started)
        if (next) setS(next)
        if (connected || Date.now() - started > 300_000) {
          if (poll.current) window.clearInterval(poll.current)
          poll.current = null
          setMsg(connected ? '' : 'Not connected yet.')
        }
      }, 3000)
    })
  }

  const revolut = s.sessions.find((x) => x.provider === 'revolut')
  const summary = !s.enabled ? 'off on this deck'
    : s.sessions.length ? s.sessions.map((x) => `${x.bank}${x.status !== 'active' ? ' (renew)' : ''}`).join(' · ')
      : 'not connected'

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left">
        <span className="flex min-w-0 items-center gap-2 text-sm text-zinc-200">
          <FileSearch size={15} className="shrink-0 text-zinc-400" /> Bank statements
          <span className="truncate text-xs text-zinc-500">{summary}</span>
        </span>
        {open ? <ChevronDown size={14} className="text-zinc-500" /> : <ChevronRight size={14} className="text-zinc-500" />}
      </button>
      {open && (
        <div className="space-y-3 border-t border-zinc-800 px-3 py-3 text-xs text-zinc-400">
          <p>
            Captain reads the outgoing payments from your account to confirm, from the bank, that a bill was paid.
            It never keeps incoming payments or balances, and it can't move money. Your bank asks you to approve
            read access; it lasts up to 180 days.
          </p>
          {!s.enabled ? (
            <p className="text-zinc-300">The deck admin turns this on with <code>FD_PAYMENTS_STATEMENTS=1</code> (it uses
              the same Enable Banking application as pay-by-bank links).</p>
          ) : (
            <>
              {s.sessions.map((x) => <SessionBlock key={`${x.provider}:${x.session_id}`} x={x} busy={busy} run={run}
                onRenew={x.provider === 'enablebanking' ? () => { setCountry(x.country || country); connect(x.bank) } : undefined} />)}
              {s.sessions.length > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                  <button disabled={busy} onClick={() => run(async () => { await syncStatements() })} className={`${BUTTON} inline-flex items-center gap-1`}>
                    {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />} Check now
                  </button>
                  <span className="text-[11px] text-zinc-500">Captain also checks at {s.sync_times.join(' and ')}.</span>
                </div>
              )}
              {!s.configured ? (
                <p className="text-zinc-300">Connecting a bank needs the deck's Enable Banking application
                  (<code>FD_ENABLEBANKING_APP_ID</code>, <code>FD_ENABLEBANKING_KEY</code>).</p>
              ) : (
                <div className="space-y-2">
                  <div className="font-medium text-zinc-200">Connect a bank account</div>
                  <div className="flex flex-wrap items-center gap-2">
                    <select value={country} onChange={(e) => setCountry(e.target.value)} className={SELECT}>
                      {COUNTRIES.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                    <select value={psuType} onChange={(e) => setPsuType(e.target.value)} className={SELECT}>
                      <option value="personal">Personal</option>
                      <option value="business">Business</option>
                    </select>
                    {banks === null ? <Loader2 className="h-4 w-4 animate-spin" /> : (
                      <select value={bank} onChange={(e) => setBank(e.target.value)} className={`${SELECT} min-w-0 max-w-full flex-1`}>
                        <option value="">Choose your bank…</option>
                        {banks.map((b) => <option key={b.name} value={b.name}>{b.name}{b.beta ? ' (beta)' : ''}</option>)}
                      </select>
                    )}
                    <button disabled={busy || !bank || !banks?.some((b) => b.name === bank)} onClick={() => connect(bank)}
                      className="rounded-md bg-sky-700 px-2.5 py-1 font-medium text-white hover:bg-sky-600 disabled:opacity-50">
                      Connect
                    </button>
                  </div>
                  {s.redirect_url && <p className="text-[11px] text-zinc-500">Redirect URL the Enable Banking application
                    must allow: <code className="break-all">{s.redirect_url}</code></p>}
                </div>
              )}
              {s.revolut_connected && (
                <label className="flex items-center gap-2 text-zinc-300">
                  <input type="checkbox" disabled={busy} checked={!!revolut} onChange={(e) => run(async () => {
                    const r = await setRevolutStatement(e.target.checked)
                    if (r.message) setMsg(r.message)
                  })} />
                  Use my Revolut Business statement too
                </label>
              )}
            </>
          )}
          {msg && <p className="text-zinc-300">{msg}</p>}
        </div>
      )}
    </div>
  )
}

function SessionBlock({ x, busy, run, onRenew }: {
  x: StatementSession; busy: boolean; run: (fn: () => Promise<unknown>) => void; onRenew?: () => void
}) {
  const ending = x.valid_until && x.valid_until * 1000 - Date.now() < 7 * 86400_000
  return (
    <div className="space-y-1.5 rounded-md border border-zinc-800 px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium text-zinc-200">{x.bank}</span>
        <span className="text-[11px] text-zinc-500">
          read {ago(x.last_sync_at)}{x.valid_until ? ` · access until ${day(x.valid_until)}` : ''}
        </span>
        <span className="ml-auto flex gap-1">
          {onRenew && (x.status !== 'active' || ending) && (
            <button disabled={busy} onClick={onRenew} className="rounded-md bg-sky-700 px-2 py-0.5 text-white hover:bg-sky-600">Renew</button>
          )}
          <button disabled={busy} onClick={() => run(() => disconnectStatements(x.provider, x.session_id))}
            className="rounded-md px-2 py-0.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200">Disconnect</button>
        </span>
      </div>
      {x.status !== 'active' && <div className="text-amber-300">Your bank's consent has ended — renew it to keep confirming bills.</div>}
      {x.last_error && x.status === 'active' && <div className="text-amber-300">{x.last_error}</div>}
      <ul className="space-y-1">
        {x.accounts.map((a) => (
          <li key={a.uid}>
            <label className="flex items-center gap-2">
              <input type="checkbox" disabled={busy} checked={a.watched}
                onChange={(e) => run(() => watchStatementAccount(a.uid, e.target.checked))} />
              <span className="text-zinc-300">{a.label}</span>
              <span className="text-[11px] text-zinc-500">{a.watched ? `${a.outgoing} outgoing payments kept` : 'not read'}</span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  )
}
