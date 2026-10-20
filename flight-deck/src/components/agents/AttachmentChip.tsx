import { FileIcon, ImageIcon, Loader2, XCircle } from 'lucide-react'
import { formatSize } from '../../services/fileTransfer'
import type { Attachment } from '../../hooks/useFileAttachments'

export function AttachmentChip({ attachment, onRemove }: { attachment: Attachment; onRemove: (id: string) => void }) {
  const isImage = attachment.type.startsWith('image/')

  return (
    <div
      className={`group relative flex items-center gap-1.5 rounded-lg border px-2 py-1.5 text-xs ${
        attachment.status === 'error'
          ? 'border-red-800 bg-red-950/30 text-red-400'
          : attachment.status === 'uploading'
          ? 'border-violet-800/50 bg-violet-950/20 text-violet-300'
          : attachment.status === 'uploaded'
          ? 'border-zinc-700 bg-zinc-800/50 text-zinc-300'
          : 'border-zinc-700 bg-zinc-800/50 text-zinc-400'
      }`}
    >
      {/* Preview or icon */}
      {attachment.preview ? (
        <img src={attachment.preview} alt="" className="h-6 w-6 rounded object-cover" />
      ) : isImage ? (
        <ImageIcon className="h-3.5 w-3.5 shrink-0" />
      ) : (
        <FileIcon className="h-3.5 w-3.5 shrink-0" />
      )}

      <span className="max-w-[120px] truncate">{attachment.name}</span>
      <span className="text-zinc-600">{formatSize(attachment.size)}</span>

      {attachment.status === 'uploading' && <Loader2 className="h-3 w-3 animate-spin text-violet-400" />}

      {/* Remove button */}
      <button
        onClick={() => onRemove(attachment.id)}
        className="ml-0.5 rounded p-0.5 text-zinc-600 hover:text-zinc-300"
      >
        <XCircle className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}
