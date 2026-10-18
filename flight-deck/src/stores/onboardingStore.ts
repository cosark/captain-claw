import { create } from 'zustand'
import { queueSave, registerHydrator } from '../services/settingsSync'
import { useAuthStore } from './authStore'

const STORAGE_KEY = 'fd:onboarding'

export interface OnboardingState {
  desktop: boolean
  forge: boolean
  council: boolean
  // The kiosk's "Getting started" guide: "Do not show again" was ticked.
  kiosk: boolean
}

interface OnboardingStore {
  completed: OnboardingState
  dismissedHints: string[]
  // Whether `completed` is this user's real state yet: with sign-in on it lives
  // in their server-side settings, which arrive after the app first renders —
  // until then a guide that opens by itself would flash for someone who opted
  // out. With sign-in off this browser's copy is the real one from the start.
  hydrated: boolean
  completeStep: (step: keyof OnboardingState) => void
  setStepCompleted: (step: keyof OnboardingState, done: boolean) => void
  dismissHint: (hintId: string) => void
  isHintDismissed: (hintId: string) => boolean
  resetOnboarding: () => void
}

function load(): { completed: OnboardingState; dismissedHints: string[] } {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    return {
      completed: {
        desktop: raw.completed?.desktop ?? false,
        forge: raw.completed?.forge ?? false,
        council: raw.completed?.council ?? false,
        kiosk: raw.completed?.kiosk ?? false,
      },
      dismissedHints: raw.dismissedHints ?? [],
    }
  } catch {
    return { completed: { desktop: false, forge: false, council: false, kiosk: false }, dismissedHints: [] }
  }
}

function save(state: { completed: OnboardingState; dismissedHints: string[] }) {
  const val = JSON.stringify(state)
  if (useAuthStore.getState().authEnabled) {
    queueSave(STORAGE_KEY, val)
  } else {
    localStorage.setItem(STORAGE_KEY, val)
  }
}

export const useOnboardingStore = create<OnboardingStore>((set, get) => {
  const initial = load()
  return {
    completed: initial.completed,
    dismissedHints: initial.dismissedHints,
    hydrated: useAuthStore.getState().authEnabled === false,

    completeStep: (step) => get().setStepCompleted(step, true),

    setStepCompleted: (step, done) => {
      if (get().completed[step] === done) return
      const completed = { ...get().completed, [step]: done }
      const state = { completed, dismissedHints: get().dismissedHints }
      save(state)
      set({ completed })
    },

    dismissHint: (hintId) => {
      if (get().dismissedHints.includes(hintId)) return
      const dismissedHints = [...get().dismissedHints, hintId]
      const state = { completed: get().completed, dismissedHints }
      save(state)
      set({ dismissedHints })
    },

    isHintDismissed: (hintId) => get().dismissedHints.includes(hintId),

    resetOnboarding: () => {
      const state = { completed: { desktop: false, forge: false, council: false, kiosk: false }, dismissedHints: [] }
      save(state)
      set(state)
    },
  }
})

// The store is created before auth status is known: once it is, an auth-off
// deck's browser copy is the real state (no server settings will come).
useAuthStore.subscribe((s, prev) => {
  if (s.authEnabled === false && prev.authEnabled !== false) useOnboardingStore.setState({ hydrated: true })
})

registerHydrator((settings) => {
  const raw = settings[STORAGE_KEY]
  if (raw) {
    try {
      const parsed = JSON.parse(raw)
      useOnboardingStore.setState({
        completed: {
          desktop: parsed.completed?.desktop ?? false,
          forge: parsed.completed?.forge ?? false,
          council: parsed.completed?.council ?? false,
          kiosk: parsed.completed?.kiosk ?? false,
        },
        dismissedHints: parsed.dismissedHints ?? [],
      })
    } catch { /* ignore */ }
  }
  // No saved value is an answer too: this user hasn't completed anything yet.
  useOnboardingStore.setState({ hydrated: true })
})
