// Fake clock: setTimeout/clearTimeout/Date.now under the test's control.
let now = 1_800_000_000_000
const timers = new Map()
let seq = 0
globalThis.setTimeout = (fn, ms = 0, ...args) => { const id = ++seq; timers.set(id, { at: now + (ms || 0), fn, args }); return id }
globalThis.clearTimeout = (id) => { timers.delete(id) }
Date.now = () => now
export function advance(ms) {
  const end = now + ms
  for (;;) {
    let next = null
    for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at || (t.at === next[1].at && id < next[0]))) next = [id, t]
    if (!next) break
    timers.delete(next[0])
    now = next[1].at
    next[1].fn(...next[1].args)
  }
  now = end
}
export const clockNow = () => now
