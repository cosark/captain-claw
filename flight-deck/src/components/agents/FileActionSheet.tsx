import { useEffect, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle, Download, Eye, Globe, Loader2, Share2, Trash2, type LucideIcon } from 'lucide-react'
import { formatSize } from '../../services/fileTransfer'

// ── A file, tapped on a phone ────────────────────────────────────────
//
// The file lists show their actions on hover, which a phone doesn't have, so
// there a tap opens this sheet: View, Download, Share — and Delete where the
// list offers it. Share hands the FILE itself to the phone's share sheet
// (Messages, Mail, WhatsApp, Files…), not its text.
//
// The share sheet only opens straight from a tap (iOS refuses it after a
// network wait), so the file is fetched as soon as the sheet opens and Share
// is ready by the time it's tapped. A very large file is fetched on the first
// tap instead, and the second tap shares it.

const PREFETCH_MAX_BYTES = 25 * 1024 * 1024

// Types a share sheet recognises, for a download served as octet-stream.
const MIME_BY_EXT: Record<string, string> = {
  '.md': 'text/markdown', '.txt': 'text/plain', '.csv': 'text/csv', '.tsv': 'text/tab-separated-values',
  '.json': 'application/json', '.html': 'text/html', '.htm': 'text/html', '.xml': 'application/xml',
  '.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml', '.heic': 'image/heic',
  '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
  '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm', '.zip': 'application/zip',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}

function mimeFor(filename: string, served: string, listed?: string): string {
  const generic = (t?: string) => !t || t === 'application/octet-stream'
  if (!generic(listed)) return listed!
  if (!generic(served)) return served
  const dot = filename.lastIndexOf('.')
  return MIME_BY_EXT[dot >= 0 ? filename.slice(dot).toLowerCase() : ''] || 'application/octet-stream'
}

type ShareState =
  | { kind: 'idle' }                 // large file: fetch on the first tap
  | { kind: 'loading' }
  | { kind: 'ready'; file: File }
  | { kind: 'unsupported' }          // this browser can't share files (of this type)
  | { kind: 'error'; message: string }

// Whether this browser can hand files to the system share sheet at all.
function canShareFiles(): boolean {
  return typeof navigator !== 'undefined' && typeof navigator.share === 'function'
    && typeof navigator.canShare === 'function'
}

export function FileActionSheet({
  filename, size, mime, icon, detail, downloadUrl, onView, onDownload, onPublicLink, onDelete, deleting = false, onClose,
}: {
  filename: string
  size: number
  /** The type the listing reports, when it has one. */
  mime?: string
  icon?: ReactNode
  /** Under the name: who created it, etc. */
  detail?: ReactNode
  /** Where the file's bytes come from (fetched for Share). */
  downloadUrl: string
  /** Absent: the file has no preview. */
  onView?: () => void
  onDownload: () => void
  /** Absent: public links are off, or not this viewer's to publish. */
  onPublicLink?: () => void
  /** Absent: not this viewer's to delete. */
  onDelete?: () => void
  deleting?: boolean
  onClose: () => void
}) {
  const [shareable] = useState(canShareFiles)
  const [share, setShare] = useState<ShareState>(
    shareable && size <= PREFETCH_MAX_BYTES ? { kind: 'loading' } : { kind: 'idle' })

  const load = async (): Promise<ShareState> => {
    try {
      const resp = await fetch(downloadUrl)
      if (!resp.ok) return { kind: 'error', message: `Couldn't fetch the file (HTTP ${resp.status})` }
      const blob = await resp.blob()
      const file = new File([blob], filename, { type: mimeFor(filename, blob.type, mime) })
      return navigator.canShare({ files: [file] }) ? { kind: 'ready', file } : { kind: 'unsupported' }
    } catch (e) {
      return { kind: 'error', message: e instanceof Error ? e.message : String(e) }
    }
  }

  // Fetch it now, so Share opens the moment it's tapped.
  useEffect(() => {
    if (!shareable || size > PREFETCH_MAX_BYTES) return
    let live = true
    void load().then((s) => { if (live) setShare(s) })
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloadUrl])

  useEffect(() => {
    // Handled here: a dialog around the sheet leaves its own Escape alone.
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); onClose() } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const doShare = () => {
    if (share.kind === 'idle' || share.kind === 'error') {
      setShare({ kind: 'loading' })
      void load().then(setShare)
      return
    }
    if (share.kind !== 'ready') return
    // Straight from the tap: no await before this call.
    navigator.share({ files: [share.file], title: filename })
      .then(onClose)
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === 'AbortError') return  // closed the share sheet
        setShare({ kind: 'error', message: e instanceof Error ? e.message : String(e) })
      })
  }

  const shareLabel = share.kind === 'loading' ? 'Preparing to share…'
    : share.kind === 'error' ? 'Try sharing again'
      : 'Share'
  const shareNote = !shareable ? ''
    : share.kind === 'idle' ? `Large file (${formatSize(size)}): it downloads first, then tap Share again.`
      : share.kind === 'unsupported' ? "This browser can't share this file. Use Download instead."
        : share.kind === 'error' ? share.message
          : ''

  return createPortal(
    <div className="fixed inset-0 z-[55] flex items-end justify-center bg-black/60" onClick={onClose}>
      <div
        role="dialog"
        aria-label={filename}
        className="w-full max-w-md rounded-t-2xl border border-zinc-800 bg-zinc-950 pb-[max(0.75rem,env(safe-area-inset-bottom))] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mx-auto mt-2 h-1 w-10 rounded-full bg-zinc-700" aria-hidden />
        <div className="flex items-start gap-3 border-b border-zinc-800 px-5 pb-4 pt-3">
          {icon && <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-zinc-900 [&_svg]:h-5 [&_svg]:w-5">{icon}</span>}
          <div className="min-w-0 flex-1">
            <div className="break-all text-[15px] font-medium text-zinc-100">{filename}</div>
            <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
              <span>{formatSize(size)}</span>
              {detail}
            </div>
          </div>
        </div>

        <div className="p-2">
          {onView && <SheetButton icon={Eye} label="View" onClick={() => { onView() }} />}
          <SheetButton icon={Download} label="Download" onClick={() => { onDownload(); onClose() }} />
          {shareable && share.kind !== 'unsupported' && (
            <SheetButton
              icon={share.kind === 'loading' ? Loader2 : Share2}
              spin={share.kind === 'loading'}
              label={shareLabel}
              onClick={doShare}
              disabled={share.kind === 'loading'}
            />
          )}
          {shareNote && (
            <p className={`flex items-start gap-1.5 px-4 pb-2 pt-1 text-xs ${share.kind === 'error' ? 'text-red-600 dark:text-red-400' : 'text-zinc-500'}`}>
              {share.kind === 'error' && <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0" />}
              {shareNote}
            </p>
          )}
          {onPublicLink && <SheetButton icon={Globe} label="Public link" onClick={onPublicLink} />}
          {onDelete && (
            <SheetButton
              icon={deleting ? Loader2 : Trash2}
              spin={deleting}
              label="Delete"
              danger
              onClick={onDelete}
              disabled={deleting}
            />
          )}
        </div>

        <div className="px-2">
          <button
            onClick={onClose}
            className="flex h-12 w-full items-center justify-center rounded-xl bg-zinc-900 text-[15px] font-medium text-zinc-300 active:bg-zinc-800"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function SheetButton({ icon: Icon, label, onClick, disabled = false, danger = false, spin = false }: {
  icon: LucideIcon
  label: string
  onClick: () => void
  disabled?: boolean
  danger?: boolean
  spin?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`flex h-12 w-full items-center gap-3.5 rounded-xl px-4 text-left text-[15px] transition-colors active:bg-zinc-800 disabled:opacity-50 ${
        danger ? 'text-red-600 dark:text-red-400' : 'text-zinc-200'
      }`}
    >
      <Icon className={`h-5 w-5 shrink-0 ${spin ? 'animate-spin' : ''} ${danger ? '' : 'text-zinc-400'}`} />
      {label}
    </button>
  )
}
