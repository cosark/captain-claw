// Revolut Business connection for payments (Phase 1). Captain asks for scope
// READ,WRITE only: it can put a payment into Revolut Business as a draft, never
// send one — the user sends the draft from the Revolut app.
//
// Steps: make a key → paste its certificate (and the redirect URI) into
// Revolut Business → paste back the Client ID → Connect (Revolut's consent
// page) → pick the EUR account to pay from.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, ChevronRight, Copy, Landmark, Loader2 } from 'lucide-react'
import {
  disconnectRevolut, getRevolut, makeRevolutKey, pasteRevolutCode, revolutAccounts, revolutConnectUrl,
  setRevolutAccount, setRevolutClient, type RevolutAccount, type RevolutConnection,
} from '../../services/payments'

function CopyBlock({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  const [done, setDone] = useState(false)
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-[11px] text-zinc-500">
        <span>{label}</span>
        <button
          type="button"
          onClick={() => navigator.clipboard?.writeText(value).then(() => { setDone(true); setTimeout(() => setDone(false), 1200) })}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-zinc-800 hover:text-zinc-300"
        >
          {done ? <Check size={12} /> : <Copy size={12} />} Copy
        </button>
      </div>
      <pre className={`max-h-28 overflow-auto whitespace-pre-wrap break-all rounded-md border border-zinc-800 bg-zinc-950 p-2 text-[11px] text-zinc-300 ${mono ? 'font-mono' : ''}`}>{value}</pre>
    </div>
  )
}

export function RevolutPanel() {
  const [conn, setConn] = useState<RevolutConnection | null>(null)
  const [open, setOpen] = useState(false)
  const [env, setEnv] = useState('production')
  const [clientId, setClientId] = useState('')
  const [code, setCode] = useState('')
  const [accounts, setAccounts] = useState<RevolutAccount[]>([])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const poll = useRef<number | null>(null)

  const refresh = useCallback(() => getRevolut().then((c) => {
    setConn(c)
    setEnv(c.environment)
    if (c.connected) revolutAccounts().then((r) => setAccounts(r.accounts)).catch(() => setAccounts([]))
    return c
  }), [])
  useEffect(() => { refresh().catch(() => setConn(null)); return () => { if (poll.current) clearInterval(poll.current) } }, [refresh])

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setMsg('')
    try { await fn() } catch (e) { setMsg(String((e as Error).message || e)) } finally { setBusy(false) }
  }

  const connect = () => run(async () => {
    const { url } = await revolutConnectUrl()
    window.open(url, '_blank', 'noopener')
    setMsg('Approve access in the Revolut tab — this panel updates when it\'s done.')
    if (poll.current) clearInterval(poll.current)
    const started = Date.now()
    poll.current = window.setInterval(async () => {
      const c = await refresh().catch(() => null)
      if (c?.connected || Date.now() - started > 180_000) {
        if (poll.current) clearInterval(poll.current)
        poll.current = null
        setMsg(c?.connected ? '' : 'Not connected yet. If Revolut showed a code, paste it below.')
      }
    }, 3000)
  })

  if (!conn) return null
  const stage = conn.connected ? 'connected' : conn.client_id ? 'consent' : conn.has_key ? 'upload' : 'start'

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left">
        <span className="flex items-center gap-2 text-sm text-zinc-200">
          <Landmark size={15} className="text-zinc-400" /> Revolut Business
          <span className="text-xs text-zinc-500">
            {conn.connected
              ? `connected${conn.environment === 'sandbox' ? ' (sandbox)' : ''} · pays from ${conn.account_label || '—'}`
              : 'not connected'}
          </span>
        </span>
        {open ? <ChevronDown size={14} className="text-zinc-500" /> : <ChevronRight size={14} className="text-zinc-500" />}
      </button>

      {open && (
        <div className="space-y-3 border-t border-zinc-800 px-3 py-3 text-xs text-zinc-400">
          <p>
            Captain puts a payment into your Revolut Business as a <b className="text-zinc-200">draft</b>; you
            send it from the Revolut app. It asks Revolut for read and draft access only (scope READ, WRITE) —
            Revolut won't let it send a payment.
          </p>

          {stage === 'start' && (
            <div className="flex flex-wrap items-center gap-2">
              <select value={env} onChange={(e) => setEnv(e.target.value)} className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100">
                <option value="production">Production</option>
                <option value="sandbox">Sandbox</option>
              </select>
              <button disabled={busy} onClick={() => run(async () => setConn(await makeRevolutKey(env)))}
                className="rounded-md bg-zinc-700 px-2.5 py-1 font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50">
                1. Make a key
              </button>
            </div>
          )}

          {stage === 'upload' && (
            <div className="space-y-2">
              <p>
                2. In Revolut Business{conn.environment === 'sandbox' ? ' (sandbox)' : ''}: Settings → APIs → Business API →
                Add API certificate. Paste the certificate, and set the OAuth redirect URI to the one below.
                Then paste the Client ID Revolut shows you.
              </p>
              <CopyBlock label="Certificate (X509 public key)" value={conn.certificate} />
              <CopyBlock label="OAuth redirect URI" value={conn.redirect_uri} />
              <div className="flex flex-wrap items-center gap-2">
                <input value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="Client ID"
                  className="w-72 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100" />
                <button disabled={busy || !clientId.trim()} onClick={() => run(async () => setConn(await setRevolutClient(clientId, conn.environment)))}
                  className="rounded-md bg-zinc-700 px-2.5 py-1 font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50">
                  Save Client ID
                </button>
              </div>
            </div>
          )}

          {stage === 'consent' && (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <button disabled={busy} onClick={connect}
                  className="rounded-md bg-sky-700 px-2.5 py-1 font-medium text-white hover:bg-sky-600 disabled:opacity-50">
                  3. Connect in Revolut
                </button>
                {poll.current && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="…or paste the code (valid 2 minutes)"
                  className="w-72 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-zinc-100" />
                <button disabled={busy || !code.trim()} onClick={() => run(async () => { setConn(await pasteRevolutCode(code)); await refresh() })}
                  className="rounded-md bg-zinc-700 px-2.5 py-1 font-medium text-zinc-100 hover:bg-zinc-600 disabled:opacity-50">
                  Use code
                </button>
              </div>
            </div>
          )}

          {stage === 'connected' && (
            <div className="flex flex-wrap items-center gap-2">
              <span>Pay from</span>
              <select value={conn.account_id} disabled={busy}
                onChange={(e) => run(async () => setConn(await setRevolutAccount(e.target.value)))}
                className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100">
                {!accounts.some((a) => a.id === conn.account_id) && <option value={conn.account_id}>{conn.account_label || '—'}</option>}
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name} · {a.balance?.toFixed?.(2)} {a.currency}</option>)}
              </select>
              <button disabled={busy} onClick={() => run(async () => { setConn(await disconnectRevolut()); setAccounts([]) })}
                className="ml-auto rounded-md px-2.5 py-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-50">
                Disconnect
              </button>
            </div>
          )}

          {(stage === 'upload' || stage === 'consent') && (
            <button disabled={busy} onClick={() => run(async () => { setConn(await disconnectRevolut()) })}
              className="text-[11px] text-zinc-500 underline-offset-2 hover:underline">
              Start over
            </button>
          )}
          {msg && <p className="text-zinc-300">{msg}</p>}
        </div>
      )}
    </div>
  )
}
