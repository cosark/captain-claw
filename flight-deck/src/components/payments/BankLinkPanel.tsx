// Pay-by-bank links (Phase 2): which bank a payment link opens. The deck's
// Enable Banking application does the PSD2 part; the user approves each
// payment in their own bank (SCA). Captain holds no bank login.
import { useCallback, useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Landmark, Loader2 } from 'lucide-react'
import {
  bankOptions, clearBankChoice, getBankLink, setBankChoice, type BankLinkStatus, type BankOption,
} from '../../services/payments'

const COUNTRIES = ['HR', 'SI', 'AT', 'DE', 'IT', 'LT', 'FI', 'FR', 'ES', 'NL', 'BE', 'PL', 'SK', 'CZ']

export function BankLinkPanel() {
  const [status, setStatus] = useState<BankLinkStatus | null>(null)
  const [open, setOpen] = useState(false)
  const [country, setCountry] = useState('HR')
  const [psuType, setPsuType] = useState('personal')
  const [banks, setBanks] = useState<BankOption[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    getBankLink().then((s) => {
      setStatus(s)
      if (s.bank) { setCountry(s.bank.country); setPsuType(s.bank.psu_type) }
    }).catch(() => setStatus(null))
  }, [])

  const loadBanks = useCallback(() => {
    if (!status?.configured) return
    setBanks(null); setMsg('')
    bankOptions(country, psuType).then((r) => setBanks(r.banks)).catch((e) => { setBanks([]); setMsg(String(e.message || e)) })
  }, [status?.configured, country, psuType])
  useEffect(() => { if (open) loadBanks() }, [open, loadBanks])

  if (!status) return null
  const choose = async (name: string) => {
    setBusy(true); setMsg('')
    try {
      const r = await setBankChoice(name, country, psuType)
      setStatus({ ...status, bank: r.bank })
    } catch (e) { setMsg(String((e as Error).message || e)) } finally { setBusy(false) }
  }

  return (
    <div className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left">
        <span className="flex items-center gap-2 text-sm text-zinc-200">
          <Landmark size={15} className="text-zinc-400" /> Pay-by-bank link
          <span className="text-xs text-zinc-500">
            {!status.configured ? 'not set up on this deck'
              : status.bank ? `opens ${status.bank.name}${status.environment === 'sandbox' ? ' (sandbox)' : ''}`
                : 'choose your bank'}
          </span>
        </span>
        {open ? <ChevronDown size={14} className="text-zinc-500" /> : <ChevronRight size={14} className="text-zinc-500" />}
      </button>

      {open && (
        <div className="space-y-3 border-t border-zinc-800 px-3 py-3 text-xs text-zinc-400">
          <p>
            Captain sends the payment to your bank and gives you one link. It opens your bank, where you
            approve the payment the usual way (m-zaba, PBZ, George…). Captain never sees your bank login.
          </p>
          {!status.configured ? (
            <p className="text-zinc-300">
              The deck admin sets this up once: an Enable Banking application, with{' '}
              <code className="text-zinc-200">FD_ENABLEBANKING_APP_ID</code> and{' '}
              <code className="text-zinc-200">FD_ENABLEBANKING_KEY</code> set on the deck.
            </p>
          ) : (
            <>
              {status.environment === 'sandbox' && (
                <p className="text-amber-300">Sandbox: links open Enable Banking's test banks — no real money moves.</p>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <select value={country} onChange={(e) => setCountry(e.target.value)} className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100">
                  {COUNTRIES.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <select value={psuType} onChange={(e) => setPsuType(e.target.value)} className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100">
                  <option value="personal">Personal account</option>
                  <option value="business">Business account</option>
                </select>
                {status.bank && (
                  <button disabled={busy} onClick={async () => { await clearBankChoice(); setStatus({ ...status, bank: null }) }}
                    className="ml-auto rounded-md px-2.5 py-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200">
                    Clear
                  </button>
                )}
              </div>
              {banks === null ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : banks.length === 0 ? (
                msg ? null : <p>No banks here can take payments through the link.</p>
              ) : (
                <div className="grid gap-1.5 sm:grid-cols-2">
                  {banks.map((b) => {
                    const chosen = status.bank?.name === b.name && status.bank?.country === country && status.bank?.psu_type === psuType
                    return (
                      <button key={b.name} disabled={busy} onClick={() => choose(b.name)}
                        className={`flex items-center gap-2 rounded-md border px-2 py-1.5 text-left ${chosen ? 'border-sky-500 bg-sky-500/10 text-zinc-100' : 'border-zinc-800 hover:bg-zinc-800 text-zinc-300'}`}>
                        {b.logo ? <img src={b.logo} alt="" className="h-5 w-5 rounded bg-white object-contain" /> : <Landmark size={16} />}
                        <span className="truncate">{b.name}</span>
                        {b.beta && <span className="ml-auto text-[10px] text-zinc-500">beta</span>}
                      </button>
                    )
                  })}
                </div>
              )}
              {status.redirect_url && (
                <p className="text-[11px] text-zinc-500">
                  Redirect URL registered in the Enable Banking application: <code>{status.redirect_url}</code>
                </p>
              )}
            </>
          )}
          {(msg || status.error) && <p className="text-zinc-300">{msg || status.error}</p>}
        </div>
      )}
    </div>
  )
}
