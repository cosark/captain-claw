// The Captain Claw desktop app (desktop/, Electron): its preload script
// exposes `window.captainClawDesktop` to the page. Absent in a browser.
//
// To try the desktop app's behaviour in a browser without building the app,
// open any deck address with `?app=desktop` — the tab then acts as the desktop
// app until it closes (`?app=web` turns it back off). Only the layout follows
// it: sign-in popups still go by the real shell.

interface DesktopBridge { isDesktop?: boolean; platform?: string; version?: string }

const SIMULATE_KEY = 'fd:simulate-desktop'

function simulated(): boolean {
  try {
    const ask = new URLSearchParams(window.location.search).get('app')
    if (ask === 'desktop') sessionStorage.setItem(SIMULATE_KEY, '1')
    else if (ask === 'web') sessionStorage.removeItem(SIMULATE_KEY)
    return sessionStorage.getItem(SIMULATE_KEY) === '1'
  } catch { return false }
}

// Read once, at load: the app rewrites the address (e.g. to /console/…) later.
const SIMULATED = simulated()

/** Running inside the desktop app (or a tab simulating it) rather than a browser tab. */
export function isDesktopApp(): boolean {
  return SIMULATED || !!(window as Window & { captainClawDesktop?: DesktopBridge }).captainClawDesktop?.isDesktop
}
