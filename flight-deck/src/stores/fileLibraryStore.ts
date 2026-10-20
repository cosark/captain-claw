import { create } from 'zustand'

/** The kiosk's File library dialog: whether it is open, and the agent it
 *  opens filtered to (none = every agent). */
interface FileLibraryStore {
  open: boolean
  agentFilter: string | null
  openLibrary: (agentSlug?: string) => void
  close: () => void
}

export const useFileLibraryStore = create<FileLibraryStore>((set) => ({
  open: false,
  agentFilter: null,
  openLibrary: (agentSlug) => set({ open: true, agentFilter: agentSlug ?? null }),
  close: () => set({ open: false, agentFilter: null }),
}))
