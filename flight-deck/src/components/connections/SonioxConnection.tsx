import { useEffect, useId, useState, type FormEvent, type ReactNode } from 'react'
import {
  Mic,
  Loader2,
  Check,
  AlertCircle,
  ChevronDown,
  Eye,
  EyeOff,
  Save,
  Trash2,
  Server,
  ExternalLink,
  ArrowDownToLine,
} from 'lucide-react'
import { useSonioxStore } from '../../stores/sonioxStore'

// Soniox: speech-to-text and the spoken voice. One key per Flight Deck user
// (per deck when sign-in is off), kept by Flight Deck. Without one, the team
// key an admin shared is used, else the server's SONIOX_API_KEY. Agents never
// see a key: Flight Deck transcribes for them, and a live mic stream gets a
// short-lived key for that one session (captain_claw/flight_deck/soniox_routes.py).

type Busy = 'save' | 'test' | 'remove' | 'import' | null
interface Notice { tone: 'ok' | 'warn'; text: string }

const pill = 'inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border'

function duration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`
}

function when(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

const SOURCE_LABEL = { own: '', shared: ' · team key', env: ' · server key' } as const

export default function SonioxConnection() {
  const { status, loading, error, refresh, saveKey, removeKey, test, importEnv } = useSonioxStore()

  const [collapsed, setCollapsed] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')
  const [reveal, setReveal] = useState(false)
  const [busy, setBusy] = useState<Busy>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const keyId = useId()

  // Flight Deck's record only (GET /fd/soniox/status) — no call to Soniox.
  useEffect(() => { refresh() }, [refresh])

  const keySet = !!status?.key_set
  const deckScope = status?.scope === 'deck'
  const source = status?.source ?? null
  const ownKey = keySet && source === 'own'
  const borrowed = keySet && !ownKey // the team key or the server's env key

  const onSave = async (e: FormEvent) => {
    e.preventDefault()
    const key = keyDraft.trim()
    if (!key || busy) return
    setBusy('save'); setNotice(null)
    const res = await saveKey(key)
    setBusy(null)
    if (!res.ok) return
    setKeyDraft('')
    setReveal(false)
    setNotice(res.warning ? { tone: 'warn', text: res.warning } : { tone: 'ok', text: 'Saved and verified with Soniox.' })
  }

  const onTest = async () => {
    if (busy) return
    setBusy('test'); setNotice(null)
    const res = await test()
    setBusy(null)
    if (res) setNotice({ tone: 'ok', text: `Soniox accepted the key and answered in ${duration(res.latency_ms)}.` })
  }

  const onRemove = async () => {
    if (busy) return
    const ok = window.confirm(
      deckScope
        ? "Remove this deck's Soniox key?\n\nVoice notes, the push-to-talk devices and spoken replies stop working until a key is added again."
        : status?.shared_available
          ? 'Remove your Soniox key?\n\nYour voice notes and agents switch to the team key your admin shared.'
          : 'Remove your Soniox key?\n\nYour voice notes and agents lose speech-to-text until you add a key again.',
    )
    if (!ok) return
    setBusy('remove'); setNotice(null)
    const done = await removeKey()
    setBusy(null)
    if (done) setNotice({ tone: 'ok', text: 'Key removed.' })
  }

  const onImport = async () => {
    if (busy) return
    setBusy('import'); setNotice(null)
    const res = await importEnv()
    setBusy(null)
    if (res.ok) {
      setNotice(res.warning
        ? { tone: 'warn', text: res.warning }
        : { tone: 'ok', text: "Moved into Flight Deck. You can remove SONIOX_API_KEY from the server's environment." })
    }
  }

  let statusPill: ReactNode
  if (!status && error && !loading) {
    statusPill = <span className={`${pill} bg-red-500/15 text-red-600 dark:text-red-500 border-red-500/30`}><AlertCircle className="h-3 w-3" /> Unavailable</span>
  } else if (!status) {
    statusPill = <span className={`${pill} bg-zinc-800 text-zinc-400 border-zinc-700`}><Loader2 className="h-3 w-3 animate-spin" /> Checking</span>
  } else if (keySet && status.verified === false && status.verify_error === 'refused') {
    statusPill = (
      <span className={`${pill} bg-red-500/15 text-red-600 dark:text-red-500 border-red-500/30`} title="Soniox refused this key — it may have been revoked.">
        <AlertCircle className="h-3 w-3" /> Key refused
      </span>
    )
  } else if (keySet && status.verified === false) {
    statusPill = (
      <span className={`${pill} bg-amber-500/15 text-amber-600 dark:text-amber-500 border-amber-500/30`} title="Saved, but Soniox couldn't be reached to check it. Test checks it again.">
        <AlertCircle className="h-3 w-3" /> Saved, not verified
      </span>
    )
  } else if (keySet) {
    statusPill = (
      <span className={`${pill} bg-emerald-500/15 text-emerald-600 dark:text-emerald-500 border-emerald-500/30`}>
        <Check className="h-3 w-3" /> Connected{source ? SOURCE_LABEL[source] : ''}
      </span>
    )
  } else {
    statusPill = <span className={`${pill} bg-zinc-800 text-zinc-400 border-zinc-700`}>Not connected</span>
  }

  let whose: string
  if (!status) whose = ''
  else if (deckScope) {
    whose = source === 'env'
      ? "Sign-in is off on this deck, so the key belongs to the deck. It still comes from the server's SONIOX_API_KEY — move it into Flight Deck to manage it here."
      : 'Sign-in is off on this deck, so the key belongs to the deck: every voice note, device and agent on it uses it.'
  } else if (source === 'shared') {
    whose = 'Your admin shared a team key, so your voice notes and agents work without a key of your own. Save your own key to use it instead.'
  } else if (source === 'env') {
    whose = "You're using the key set on the server. Save your own key to use it instead."
  } else if (status.shared_available) {
    whose = 'Your own key: your voice notes and agents use it instead of the team key your admin shared.'
  } else {
    whose = 'Your own key: your voice notes, devices and agents use it.'
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 overflow-hidden">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className={'w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-zinc-900/80 transition-colors ' + (collapsed ? '' : 'border-b border-zinc-800')}
        aria-expanded={!collapsed}
      >
        <div className="h-10 w-10 shrink-0 rounded-md bg-gradient-to-br from-sky-500/20 to-violet-500/20 border border-zinc-800 flex items-center justify-center">
          <Mic className="h-5 w-5 text-zinc-200" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-zinc-100">Soniox</h3>
            {statusPill}
          </div>
          <div className="text-xs text-zinc-500 mt-0.5">Speech-to-text and the spoken voice</div>
        </div>
        {loading && status && <Loader2 className="h-4 w-4 shrink-0 animate-spin text-zinc-500" />}
        <ChevronDown className={'h-4 w-4 shrink-0 text-zinc-500 transition-transform ' + (collapsed ? '-rotate-90' : '')} />
      </button>

      <div hidden={collapsed} className="px-5 py-4 space-y-4">
        <p className="text-xs leading-relaxed text-zinc-400">
          Soniox turns speech into text and text into speech. It transcribes your WhatsApp voice notes, the
          push-to-talk on your StickS3 or StopWatch and the audio in videos you send, and your agents use it
          for the <code className="text-zinc-300">stt</code> tool and the microphone. Flight Deck keeps the key.
          Agents never see it: Flight Deck transcribes for them, and a live microphone gets a key that expires
          after that one session.
        </p>

        {error && (
          <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            <span className="min-w-0 flex-1 break-words">{error}</span>
            {!status && !loading && (
              <button type="button" onClick={() => refresh()} className="shrink-0 underline hover:no-underline">Try again</button>
            )}
          </div>
        )}

        {notice && (
          <div className={'rounded-md border px-3 py-2 text-xs break-words ' + (notice.tone === 'ok'
            ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
            : 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300')}
          >
            {notice.text}
          </div>
        )}

        {status && (
          <form onSubmit={onSave} className="space-y-2">
            <label htmlFor={keyId} className="block text-xs font-medium text-zinc-400">
              API key
              {ownKey && <span className="ml-2 text-[10px] text-emerald-600 dark:text-emerald-500">(saved)</span>}
              {source === 'shared' && <span className="ml-2 text-[10px] text-emerald-600 dark:text-emerald-500">(team key shared by your admin)</span>}
              {source === 'env' && <span className="ml-2 text-[10px] text-emerald-600 dark:text-emerald-500">(the server's key)</span>}
            </label>
            <div className="relative">
              <input
                id={keyId}
                type={reveal ? 'text' : 'password'}
                value={keyDraft}
                onChange={(e) => setKeyDraft(e.target.value)}
                placeholder={
                  borrowed
                    ? 'Paste a key of your own to use it instead'
                    : keySet
                      ? `•••• saved (${status.key_hint || '····'}) — paste a new key to replace`
                      : 'Paste your Soniox API key'
                }
                autoComplete="off"
                spellCheck={false}
                disabled={busy === 'save'}
                className="w-full min-w-0 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 pr-9 text-sm text-zinc-200 font-mono placeholder-zinc-600 focus:border-violet-500 focus:outline-none disabled:opacity-50"
              />
              <button
                type="button"
                onClick={() => setReveal((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
                tabIndex={-1}
                aria-label={reveal ? 'Hide key' : 'Show key'}
              >
                {reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </button>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="submit"
                disabled={!keyDraft.trim() || busy !== null}
                className="inline-flex items-center gap-2 rounded-md bg-violet-600 hover:bg-violet-500 px-3 py-1.5 text-sm text-white disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
                title="Flight Deck checks the key with Soniox before saving it"
              >
                {busy === 'save' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                {busy === 'save' ? 'Verifying…' : 'Save'}
              </button>
              {keySet && (
                <button
                  type="button"
                  onClick={onTest}
                  disabled={busy !== null}
                  className="inline-flex items-center gap-1.5 rounded-md bg-zinc-800 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
                  title="Check the key with Soniox"
                >
                  {busy === 'test' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Server className="h-3 w-3" />}
                  Test
                </button>
              )}
              {deckScope && source === 'env' && (
                <button
                  type="button"
                  onClick={onImport}
                  disabled={busy !== null}
                  className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-40"
                  title="Save the server's SONIOX_API_KEY as this deck's key"
                >
                  {busy === 'import' ? <Loader2 className="h-3 w-3 animate-spin" /> : <ArrowDownToLine className="h-3 w-3" />}
                  Move into Flight Deck
                </button>
              )}
              {ownKey && (
                <button
                  type="button"
                  onClick={onRemove}
                  disabled={busy !== null}
                  className="inline-flex items-center gap-1.5 rounded-md border border-red-500/30 hover:bg-red-500/10 px-3 py-1.5 text-xs text-red-600 dark:text-red-500 disabled:opacity-50"
                >
                  {busy === 'remove' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
                  Remove key
                </button>
              )}
            </div>

            {keySet && (
              <div className="text-[11px] leading-snug text-zinc-500">
                {source === 'shared' ? 'Team key' : source === 'env' ? 'Server key' : 'Key'}{' '}
                <code className="text-zinc-300">{status.key_hint || '····'}</code>
                {status.verified === true
                  ? <> · verified{status.verified_at ? ` ${when(status.verified_at)}` : ''}</>
                  : status.verified === false && status.verify_error === 'refused'
                    ? <> · <span className="text-red-600 dark:text-red-400">Soniox refused it</span> — {borrowed ? 'tell your admin, or paste a key of your own above.' : 'paste a new key above.'}</>
                    : status.verified === false
                      ? <> · not verified — Soniox couldn't be reached to check it. Test checks it again.</>
                      : <> · not checked yet</>}
              </div>
            )}

            <div className="text-[11px] leading-snug text-zinc-500">
              {whose}{' '}
              Get a key at{' '}
              <a href="https://console.soniox.com" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 text-violet-500 hover:underline">
                console.soniox.com <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
