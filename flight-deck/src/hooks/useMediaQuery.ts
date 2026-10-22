import { useSyncExternalStore } from 'react'

// A phone either way up: narrow, or — turned to landscape, where even a
// small phone is 900+ px wide — a touch screen that is short. Tablets are
// taller than 500px both ways, so they stay tablets.
const MOBILE = '(max-width: 767px), (pointer: coarse) and (max-height: 500px)'
const TABLET = '(min-width: 768px) and (max-width: 1023px)'
const DESKTOP = '(min-width: 1024px)'

function subscribe(query: string) {
  return (cb: () => void) => {
    const mql = window.matchMedia(query)
    mql.addEventListener('change', cb)
    return () => mql.removeEventListener('change', cb)
  }
}

function snapshot(query: string) {
  return () => window.matchMedia(query).matches
}

const server = () => false

export function useIsMobile() {
  const isMobile = useSyncExternalStore(subscribe(MOBILE), snapshot(MOBILE), server)
  const tablet = useSyncExternalStore(subscribe(TABLET), snapshot(TABLET), server)
  const desktop = useSyncExternalStore(subscribe(DESKTOP), snapshot(DESKTOP), server)
  // A landscape phone is a phone, whatever its width says.
  return { isMobile, isTablet: tablet && !isMobile, isDesktop: desktop && !isMobile }
}

// A phone or tablet's on-screen keyboard: Enter there starts a new line (the
// Send button sends), as in any messaging app.
export function touchKeyboard(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(pointer: coarse) and (hover: none)').matches
}
