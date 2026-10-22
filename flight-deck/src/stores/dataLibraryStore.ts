import { create } from 'zustand'

/** The kiosk's Data library dialog: whether it is open, and the agent it
 *  opens filtered to ('' = every agent). */
interface DataLibraryStore {
  open: boolean
  agentFilter: string
  openData: (agentSlug?: string) => void
  close: () => void
}

export const useDataLibraryStore = create<DataLibraryStore>((set) => ({
  open: false,
  agentFilter: '',
  openData: (agentSlug) => set({ open: true, agentFilter: agentSlug ?? '' }),
  close: () => set({ open: false, agentFilter: '' }),
}))
