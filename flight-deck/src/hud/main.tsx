// Entry for hud.html — the smart-glasses HUD. Deliberately tiny: React, the
// auth store and the HUD modules only (no Tailwind, KaTeX or dashboard code).
// No StrictMode: its dev double-effects would open two chat sockets and start
// two pairing codes on the glasses.

import { createRoot } from 'react-dom/client'
import { IS_APP } from './device'
import './hud.css'
import { HudApp } from './HudApp'

createRoot(document.getElementById('root')!).render(<HudApp />)

// Warm launches: cache the hashed /assets/* bundle (immutable) so a relaunch
// over the glasses' slow link only fetches the HTML. Scope /hud/ only.
// Not in the Android app: every app start must load the deck's current HUD,
// never a cached shell (the app shows its own screen when the deck is
// unreachable) — so it also drops a worker an earlier build registered.
if ('serviceWorker' in navigator && window.location.pathname.startsWith('/hud')) {
  window.addEventListener('load', () => {
    if (IS_APP) {
      navigator.serviceWorker.getRegistrations()
        .then((regs) => Promise.all(regs.map((r) => r.unregister())))
        .catch(() => { /* optional */ })
      return
    }
    navigator.serviceWorker.register('/hud/sw.js', { scope: '/hud/' }).catch(() => { /* optional */ })
  })
}
