import { create } from 'zustand'

/** The kiosk's Data library dialog: whether it is open. */
interface DataLibraryStore {
  open: boolean
  openData: () => void
  close: () => void
}

export const useDataLibraryStore = create<DataLibraryStore>((set) => ({
  open: false,
  openData: () => set({ open: true }),
  close: () => set({ open: false }),
}))
