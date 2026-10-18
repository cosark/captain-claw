# Kiosk guide screenshots

The kiosk's Getting started guide (`src/components/layout/KioskOnboardingDialog.tsx`)
shows real screenshots of the kiosk, in the viewer's theme:
`src/assets/onboarding/<screen>-{dark,light}.webp`. When those screens change,
regenerate them here.

`run.sh` does the following:

1. Boots a throwaway Flight Deck with auth on (data in `.run/`).
2. Sets it up as an admin would: kiosk mode for selected users, a kiosk user
   (Maya), placeholder Google and Microsoft OAuth clients, and a team Telegram
   bot (written to the deck's settings directly: the admin route checks the
   token with Telegram).
3. Starts two demo agents (`mock_agent.py`: chat, files, datastore, with a
   conversation already in them) and records them, plus a stopped one, as
   Maya's agents.
4. Runs `shots.cjs`, which signs in as Maya and shoots each screen in both
   themes; `to_webp.py` writes them into `src/assets/onboarding/`.

With `MODE=guide`, `shots.cjs` instead shoots the guide itself (wide in both
themes, and a phone) into `.run/guide/` and checks how it behaves: it opens by
itself on a first visit, every step's screenshot loads, a step's button opens
the right Connections tab, Info and Settings reopen it, and "Do not show
again" is saved to the user's settings and holds across a reload. The results
land in `.run/report.json`.

## Run

```bash
cd flight-deck && npm run build                 # the deck serves the built static/
cd .. && flight-deck/e2e/onboarding/run.sh      # new screenshots → rebuild after
MODE=guide flight-deck/e2e/onboarding/run.sh    # the guide's check
```

Requirements: `uv` (Python env), Node 22+, and Playwright with Chromium
(`npm i -g playwright`), or set `PLAYWRIGHT_MODULE=/path/to/node_modules/playwright`.
Ports: `FD_PORT` (25191) and `AGENT_PORT` (24191, and the next one).
