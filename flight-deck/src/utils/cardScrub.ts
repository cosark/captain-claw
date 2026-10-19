// Keep payment-card data out of Captain — the browser's copy of
// captain_claw/card_scrub.py (same rules; both are held to
// tests/fixtures/card_scrub_cases.json by scripts/card-scrub-check.mjs).
// Runs before a chat message is shown, saved or sent, so Flight Deck's own
// chat log never holds a card number. Captain never needs one: payments are
// approved in the user's bank app.

export const NOTE =
  '[Note: a payment card number was removed from this message. Captain never needs card numbers or CVVs.]'

export interface CardFinding { kind: 'pan' | 'cvv' | 'expiry'; start: number; end: number; last4: string }

const CANDIDATE_RE = /(?<![A-Za-z0-9])\d(?:[ -]?\d){11,18}(?![A-Za-z0-9])/g
const CVV_LABEL_RE = /\b(?:cvv2?|cvc2?|csc|cid|sigurnosni\s+(?:kod|broj))\b\s*(?:[:=#]|je|is)?\s*(\d{3,4})\b/gi
const EXPIRY_RE = /(?<![\d/])(0[1-9]|1[0-2])\s?\/\s?(\d{2}|20\d{2})(?![\d/])/g
const EXPIRY_LABEL_RE = /\b(?:exp(?:iry|ires)?|valid\s+thru|vrijedi\s+do|istek)\b[:.\s]*$/i
const REF_CONTEXT_RE = /(?:[A-Z]{2}\d{2}[ -]?|poziv\s+na\s+broj\s*:?\s*)$/i
const BARE_CVV_RE = /(?<![\d/.,])\d{3,4}(?![\d/.,])/g

export function luhnOk(digits: string): boolean {
  let total = 0
  for (let i = 0; i < digits.length; i++) {
    let n = Number(digits[digits.length - 1 - i])
    if (i % 2 === 1) n = n > 4 ? n * 2 - 9 : n * 2
    total += n
  }
  return total % 10 === 0
}

function networkOk(d: string): boolean {
  const n = d.length
  const p2 = Number(d.slice(0, 2)), p3 = Number(d.slice(0, 3)), p4 = Number(d.slice(0, 4))
  if (d[0] === '4') return n === 13 || n === 16 || n === 19                     // Visa
  if ((p2 >= 51 && p2 <= 55) || (p4 >= 2221 && p4 <= 2720)) return n === 16      // Mastercard
  if (p2 === 34 || p2 === 37) return n === 15                                    // Amex
  if (p4 === 6011 || (p3 >= 644 && p3 <= 649) || p2 === 65) return n >= 16 && n <= 19  // Discover
  if (p4 >= 3528 && p4 <= 3589) return n >= 16 && n <= 19                        // JCB
  if ((p3 >= 300 && p3 <= 305) || p2 === 36 || p2 === 38 || p2 === 39) return n >= 14 && n <= 19  // Diners
  if (p2 === 62) return n >= 16 && n <= 19                                       // UnionPay
  if (p2 === 50 || (p2 >= 56 && p2 <= 69)) return n >= 12 && n <= 19             // Maestro
  return false
}

function groupingOk(raw: string): boolean {
  const seps = new Set(raw.match(/[ -]/g) ?? [])
  if (seps.size === 0) return true
  if (seps.size > 1) return false
  const groups = raw.split(/[ -]/).map((g) => g.length)
  const same = (a: number[]) => a.length === groups.length && a.every((v, i) => v === groups[i])
  if (same([4, 6, 5]) || same([4, 6, 4])) return true
  return groups.length >= 2 && groups.slice(0, -1).every((g) => g === 4)
    && groups[groups.length - 1] >= 1 && groups[groups.length - 1] <= 4
}

export function findCardData(text: string): CardFinding[] {
  const found: CardFinding[] = []
  for (const m of text.matchAll(CANDIDATE_RE)) {
    const start = m.index ?? 0
    if (REF_CONTEXT_RE.test(text.slice(Math.max(0, start - 20), start))) continue
    const raw = m[0]
    const ends = [...raw.matchAll(/\d+/g)].map((g) => (g.index ?? 0) + g[0].length)
    for (let i = ends.length - 1; i >= 0; i--) {
      const part = raw.slice(0, ends[i])
      const digits = part.replace(/\D/g, '')
      if (digits.length >= 12 && digits.length <= 19 && groupingOk(part) && luhnOk(digits) && networkOk(digits)) {
        found.push({ kind: 'pan', start, end: start + ends[i], last4: digits.slice(-4) })
        break
      }
    }
  }
  const pans = [...found]
  const nearPan = (s: number, e: number, reach: number) => pans.some((p) => s - reach <= p.end && e + reach >= p.start)
  const taken = (s: number, e: number) => found.some((f) => s < f.end && e > f.start)

  for (const m of text.matchAll(EXPIRY_RE)) {
    const s = m.index ?? 0, e = s + m[0].length
    const labelled = EXPIRY_LABEL_RE.test(text.slice(Math.max(0, s - 20), s))
    if ((labelled || nearPan(s, e, 40)) && !taken(s, e)) found.push({ kind: 'expiry', start: s, end: e, last4: '' })
  }
  for (const m of text.matchAll(CVV_LABEL_RE)) {
    const e = (m.index ?? 0) + m[0].length
    const s = e - m[1].length
    if (!taken(s, e)) found.push({ kind: 'cvv', start: s, end: e, last4: '' })
  }
  for (const pan of pans) {
    const lineStart = text.lastIndexOf('\n', pan.start - 1) + 1
    const nl = text.indexOf('\n', pan.end)
    const lineEnd = nl < 0 ? text.length : nl
    const line = text.slice(lineStart, lineEnd)
    for (const m of line.matchAll(BARE_CVV_RE)) {
      const s = lineStart + (m.index ?? 0), e = s + m[0].length
      if (!taken(s, e)) found.push({ kind: 'cvv', start: s, end: e, last4: '' })
    }
  }
  return found.sort((a, b) => a.start - b.start)
}

function replacement(f: CardFinding): string {
  if (f.kind === 'pan') return `[card number removed · ends ${f.last4}]`
  return f.kind === 'cvv' ? '[CVV removed]' : '[expiry removed]'
}

export function scrubCardData(text: string): { text: string; found: CardFinding[] } {
  const found = findCardData(text)
  let out = text
  for (const f of [...found].sort((a, b) => b.start - a.start)) {
    out = out.slice(0, f.start) + replacement(f) + out.slice(f.end)
  }
  return { text: out, found }
}

/** A person's message with card data removed, and a note saying so. */
export function scrubUserText(text: string): string {
  if (!text || !/\d/.test(text)) return text
  const { text: out, found } = scrubCardData(text)
  return found.length ? `${out}\n\n${NOTE}` : text
}
