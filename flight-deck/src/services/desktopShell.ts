// The Captain Claw desktop app (desktop/, Electron): its preload script
// exposes `window.captainClawDesktop` to the page. Absent in a browser.

interface DesktopBridge { isDesktop?: boolean; platform?: string; version?: string }

/** Running inside the desktop app rather than a browser tab. */
export function isDesktopApp(): boolean {
  return !!(window as Window & { captainClawDesktop?: DesktopBridge }).captainClawDesktop?.isDesktop
}
