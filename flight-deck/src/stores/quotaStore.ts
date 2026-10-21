import { create } from 'zustand'
import { registerSignOutTeardown } from './authStore'
import { getMyQuota, spendFreeReset, type QuotaStatus } from '../services/profile'

// The signed-in user's daily plan allowances (/fd/quota): messages and AI
// tokens today, and when they reset. Flight Deck pushes new totals on the chat
// socket (`fd_quota` after each counted message, the totals on a refused
// one); the tokens of a turn arrive from the agent a moment after it ends, so
// a finished turn asks again shortly after. A free reset (an admin gives them)
// starts the day's allowances over.

interface QuotaStore {
  status: QuotaStatus | null
  set: (status: QuotaStatus) => void
  refresh: () => Promise<void>
  /** Re-read shortly — after a turn, once the agent has reported its tokens. */
  refreshSoon: () => void
  /** Spend one free reset; throws with the server's reason when it can't. */
  spendReset: () => Promise<void>
}

let inflight: Promise<void> | null = null
let soonTimer: ReturnType<typeof setTimeout> | null = null
let resetTimer: ReturnType<typeof setTimeout> | null = null

export const useQuotaStore = create<QuotaStore>((set, get) => ({
  status: null,

  set: (status) => {
    set({ status })
    // Ask again just after the day turns over, so a spent allowance lifts on time.
    if (resetTimer) clearTimeout(resetTimer)
    const wait = new Date(status.resets_at).getTime() - Date.now() + 2000
    if (Number.isFinite(wait) && wait > 0 && wait < 25 * 3600 * 1000) {
      resetTimer = setTimeout(() => { void get().refresh() }, wait)
    }
  },

  refresh: () => {
    if (!inflight) {
      inflight = getMyQuota()
        .then((s) => get().set(s))
        .catch(() => { /* keep what we have */ })
        .finally(() => { inflight = null })
    }
    return inflight
  },

  refreshSoon: () => {
    if (soonTimer) clearTimeout(soonTimer)
    soonTimer = setTimeout(() => { soonTimer = null; void get().refresh() }, 2500)
  },

  spendReset: async () => {
    try {
      get().set(await spendFreeReset())
    } catch (e) {
      void get().refresh()  // e.g. spent in another tab: show what's left
      throw e
    }
  },
}))

registerSignOutTeardown(() => {
  if (soonTimer) clearTimeout(soonTimer)
  if (resetTimer) clearTimeout(resetTimer)
  soonTimer = resetTimer = null
  useQuotaStore.setState({ status: null })
})

/** Is anything about today's allowances worth showing? (Not for admins or
 *  unlimited plans.) */
export function quotaLimited(s: QuotaStatus | null): s is QuotaStatus {
  return !!s && !s.exempt && (s.messages.limit > 0 || s.tokens.limit > 0)
}

/** Could the person spend a free reset now? (They hold one and used something today.) */
export function canSpendReset(s: QuotaStatus | null): boolean {
  return quotaLimited(s) && (s.resets_left ?? 0) > 0 && (s.messages.used > 0 || s.tokens.used > 0)
}
