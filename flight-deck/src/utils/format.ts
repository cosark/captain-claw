/** A token count, short: 950 · 1.2k · 48k. */
export function fmtTokens(n?: number): string {
  const v = n || 0
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k`
  return String(v)
}
