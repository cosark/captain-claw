import { defaultUrlTransform } from 'react-markdown'
import type { AgentFile } from '../services/fileTransfer'
import { isMemberCreated } from './sharedWorkspace'

// How Flight Deck shows a page or markdown somebody else may have written —
// the file viewer and the library's preview alike.

/** Whether a file must be shown as someone else's: no scripts, no remote
 *  content. A 'shared' file is trusted only when it says it is the viewer's
 *  own; any other is untrusted when a member wrote it. */
export function isUntrustedFile(file: AgentFile): boolean {
  return file.source === 'shared' ? file.created_by?.kind !== 'me' : isMemberCreated(file.created_by)
}

// Markdown somebody else wrote (another member's file, or a member's file in
// the owner's panels) must not make the viewer's browser fetch anything — a
// tracking pixel would tell its author who opened it, and when. So it shows
// no images at all: a remote one plainly, and a relative one too, since a
// same-origin address can redirect anywhere (and a relative src resolves
// against Flight Deck's page, never the file's folder, so it never showed the
// picture anyway). Links still render.
export function untrustedUrlTransform(url: string, key: string): string {
  if (key === 'src') return ''
  return defaultUrlTransform(url)
}

// HTML somebody else wrote renders inert: no scripts (an empty sandbox), and a
// CSP that lets it load nothing from anywhere — no remote image, stylesheet or
// font that would tell its author who opened it. Inline styles and data:
// images still show. The viewer's own (or, in the owner's panels, the owner's)
// HTML keeps running scripts — never same-origin.
const INERT_HTML_CSP = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">`

export function htmlFrame(content: string, untrusted: boolean): { srcDoc: string; sandbox: string } {
  return untrusted
    ? { srcDoc: INERT_HTML_CSP + content, sandbox: '' }
    : { srcDoc: content, sandbox: 'allow-scripts' }
}
