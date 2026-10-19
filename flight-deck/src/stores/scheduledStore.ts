import { create } from 'zustand'
import { schedulerApi, type ScheduledJob } from '../services/schedulerApi'

/** The kiosk's Scheduled tasks dialog: whether it is open, what it opens on
 *  (a task a chat message points at, or a new task for an agent), and the
 *  task list the entry buttons count. */
interface ScheduledStore {
  open: boolean
  /** Opened from a chat message's "Scheduled" chip: show this task. */
  focusTaskId: string | null
  /** Opened as "new task" for this agent slug. */
  newForAgent: string | null
  jobs: ScheduledJob[]
  loaded: boolean
  openTasks: (taskId?: string) => void
  openNew: (agentSlug?: string) => void
  close: () => void
  fetchJobs: () => Promise<void>
}

export const useScheduledStore = create<ScheduledStore>((set) => ({
  open: false,
  focusTaskId: null,
  newForAgent: null,
  jobs: [],
  loaded: false,
  openTasks: (taskId) => set({ open: true, focusTaskId: taskId ?? null, newForAgent: null }),
  openNew: (agentSlug) => set({ open: true, focusTaskId: null, newForAgent: agentSlug ?? '' }),
  close: () => set({ open: false, focusTaskId: null, newForAgent: null }),
  fetchJobs: async () => {
    try {
      const jobs = await schedulerApi.listJobs()
      set({ jobs: Array.isArray(jobs) ? jobs : [], loaded: true })
    } catch {
      set({ loaded: true })
    }
  },
}))

/** Tasks only (jobs the kiosk made), not WhatsApp/channel jobs from the full Scheduler page. */
export function isTask(j: ScheduledJob): boolean {
  return j.delivery_kind === 'agent'
}
