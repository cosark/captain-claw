import { useCallback, useState } from 'react'
import { uploadFileToAgent } from '../services/fileTransfer'
import { imageRef } from '../services/agentChat'

/** Where an agent's web server is: what an upload goes to. */
export interface AgentConn { host: string; port: number; auth: string }

export interface Attachment {
  id: string
  file: File
  name: string
  size: number
  type: string
  preview?: string // data URL for images
  status: 'pending' | 'uploading' | 'uploaded' | 'error'
  uploadedPath?: string
  error?: string
}

let attachId = 0
function nextAttachId() { return `attach-${Date.now()}-${++attachId}` }

// Build the outgoing message text, appending references to any uploaded files
// so the agent can locate them. Images use the "[Attached image: …]" ref,
// which travels as the chat frame's image_paths (see splitImageRefs): the
// agent shows them to its own model when it sees images, and otherwise has a
// vision model describe them before the turn.
export function appendAttachmentRefs(text: string, uploadedFiles: Attachment[]): string {
  if (uploadedFiles.length === 0) return text
  const fileRefs = uploadedFiles.map((a) =>
    a.type.startsWith('image/')
      ? imageRef(a.uploadedPath || '')
      : `[Attached file: ${a.name} → ${a.uploadedPath}]`,
  ).join('\n')
  return text ? `${text}\n\n${fileRefs}` : fileRefs
}

// Shared file-attachment state + handlers for the chat and queue inputs (and
// a skill card's input dialog).
// Files upload immediately on add; callers read `attachments` (filtering to
// status==='uploaded') and bake refs into the outgoing text via
// appendAttachmentRefs.
export function useFileAttachments(conn: AgentConn | null) {
  const [attachments, setAttachments] = useState<Attachment[]>([])

  const uploadAttachment = useCallback(async (att: Attachment) => {
    if (!conn) return
    setAttachments((prev) => prev.map((a) => a.id === att.id ? { ...a, status: 'uploading' } : a))
    try {
      const result = await uploadFileToAgent(conn.host, conn.port, conn.auth, att.file)
      setAttachments((prev) => prev.map((a) => a.id === att.id ? { ...a, status: 'uploaded', uploadedPath: result.path } : a))
    } catch (err) {
      setAttachments((prev) => prev.map((a) => a.id === att.id ? { ...a, status: 'error', error: String(err) } : a))
    }
  }, [conn])

  const addFiles = useCallback((files: FileList | File[]) => {
    const newAttachments: Attachment[] = Array.from(files).map((file) => {
      const att: Attachment = {
        id: nextAttachId(),
        file,
        name: file.name,
        size: file.size,
        type: file.type,
        status: 'pending',
      }
      // Generate preview for images
      if (file.type.startsWith('image/')) {
        const reader = new FileReader()
        reader.onload = (e) => {
          setAttachments((prev) => prev.map((a) => a.id === att.id ? { ...a, preview: e.target?.result as string } : a))
        }
        reader.readAsDataURL(file)
      }
      return att
    })
    setAttachments((prev) => [...prev, ...newAttachments])
    newAttachments.forEach((att) => uploadAttachment(att))
  }, [uploadAttachment])

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id))
  }, [])

  const clearAttachments = useCallback(() => setAttachments([]), [])

  // Paste event on a textarea. Returns true if clipboard files were extracted
  // (caller should NOT also handle text); false → let the default text paste run.
  const handlePasteEvent = useCallback((e: React.ClipboardEvent): boolean => {
    const items = e.clipboardData.items
    const files: File[] = []
    for (let i = 0; i < items.length; i++) {
      const item = items[i]
      if (item.kind === 'file') {
        const file = item.getAsFile()
        if (file) files.push(file)
      }
    }
    if (files.length > 0) {
      e.preventDefault()
      addFiles(files)
      return true
    }
    return false
  }, [addFiles])

  // Clipboard-button paste via the async Clipboard API. Files → addFiles;
  // otherwise the text is handed to onText for the caller to insert.
  const pasteFromClipboard = useCallback(async (onText: (text: string) => void) => {
    try {
      const items = await navigator.clipboard.read()
      const files: File[] = []
      for (const item of items) {
        for (const type of item.types) {
          if (type.startsWith('image/') || type === 'application/octet-stream') {
            const blob = await item.getType(type)
            const ext = type.split('/')[1] || 'png'
            const file = new File([blob], `clipboard-${Date.now()}.${ext}`, { type })
            files.push(file)
          }
        }
      }
      if (files.length > 0) {
        addFiles(files)
      } else {
        const text = await navigator.clipboard.readText()
        if (text) onText(text)
      }
    } catch {
      // Fallback to text
      try {
        const text = await navigator.clipboard.readText()
        if (text) onText(text)
      } catch { /* clipboard not available */ }
    }
  }, [addFiles])

  return { attachments, addFiles, removeAttachment, clearAttachments, handlePasteEvent, pasteFromClipboard }
}
