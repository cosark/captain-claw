# Captain Claw · handheld voice client (M5Stack StickS3, StopWatch and PaperColor)

Hold the talk button, talk, release. Flight Deck transcribes it, sends it to
one of your agents, and the device shows the reply, reads it aloud, and shows
any picture the agent made.

Server side: `captain_claw/flight_deck/device_bridge.py` (`/device/ws`).

One source tree builds both devices (`src/board.h` holds what differs):

| | StickS3 | StopWatch | PaperColor |
|---|---|---|---|
| screen | 240×135 LCD (`ui_stick.cpp`) | 466×466 round AMOLED (`ui_round.cpp`) | 4" colour e-paper, 400×600, six inks (`ui_paper.cpp`) |
| talk button (A) | front | yellow | A |
| other button (B) | side | the second button | B |
| third button (C) | — | — | C: back · hold = home screen |
| extras | — | vibration (buzz before speech, on talk), clock | two status lights, room temperature + humidity, keeps its screen asleep |
| build | `pio run -e sticks3` | `pio run -e stopwatch` | `pio run -e papercolor` |

The sections below say "front"/"side" for A/B.

## What the agent learns about the device

Every turn goes up with the device's readings at that moment
(`telemetry.cpp`): battery (%, volts, charging or on USB), Wi-Fi (network and
signal), how it's held and whether it's moving (StickS3/StopWatch IMU: lying
flat / tilted / held upright · still / moving / shaking), the room's
temperature and humidity (PaperColor SHT40), and the local time. Flight Deck
turns them into one line of context (`device_context_line()`), so "how's my
battery?" or "is it warm in here?" just work. The agent doesn't recite them
unless asked, or unless the battery is nearly empty. They are never stored as
the user's words.

## Talk from sleep

Asleep, hold the talk button and start speaking at once. The device records
while it wakes up and reconnects (a few seconds), then sends all of it as if
you had spoken just now. A short press only wakes it.

## PaperColor (colour e-paper)

The paper takes 15–30 s to redraw and keeps its picture with no power at all,
so it shows what lasts and leaves the moment to sound and light:

- **Status lights:** red while it listens (brighter when you're louder), amber
  breathing while the agent works, cyan with the voice, a soft blue glow while
  the paper redraws, white blinks as the cue before speech. Menu → *Status
  lights* sets off / low / bright.
- **The answer card:** what you asked (blue), the agent's picture in colour
  (dithered into the six inks), then the answer. Long answers turn pages
  (B / C). In landscape the picture keeps a column of its own.
- **Home screen:** the date, the room (temperature, humidity), whether it's
  ready, and the answer it keeps: the newest one, or one you pinned
  (Menu → *Pin answer*). Before any answer, it shows your character.
- **Asleep** (after 1 / 3 / 10 idle minutes) the paper keeps the home screen
  with "Asleep · hold A and talk". It redraws it every 30 min / 1 h / 3 h
  (Menu → *Redraw asleep*) without Wi-Fi, for the date, the room and the
  battery. The last and the pinned answer live in flash, so they survive the
  sleep. Press C after waking to read the last answer again.
- **Menus are slow to draw, so they talk:** each press clicks, and the
  highlighted row is read aloud by Flight Deck (*Read menu aloud*). The paper
  catches up once you stop pressing.
- **Settings on your phone:** Menu → *Settings on phone* shows a QR code. Open
  it on a phone on the same Wi-Fi to change every setting there. The link
  carries a one-time key and closes when you leave that screen.
- **Orientation:** portrait or landscape, either way up (Menu →
  *Orientation*), because the buttons' best side is a matter of how you hold it.
- **Battery empty:** it leaves "Battery empty" on the paper and switches off.

| | A | B | C |
|---|---|---|---|
| hold | talk (release sends) | menu (hold again closes) | home screen |
| click | stop speaking · picture ↔ text · choose (menus) | next page / next row | previous page / previous row / back |
| double-click (on an answer) | | hear the answer again | |

To see every paper screen without the device (Mac, SDL2 from Homebrew):

```bash
pio run -e preview_paper
uv run --no-project --with pillow python preview/render.py paper   # → preview/out/paper/sheet.png
```

## StopWatch screen

The round screen follows M5Stack's own StopWatch look: black, a glowing badge
in the middle with a name under it, page dots and chevrons for lists. The ring
round the edge shows what the watch is doing — red while it listens (filling
toward the 110 s recording limit), amber while the agent works (a tick per
tool step), cyan while it speaks. The clock sits at the top between the Wi-Fi
(left) and agent (right) dots; its time zone is set in the setup page.
Text uses Inter (`fonts/`, made by `tools/make_fonts.py`; SIL OFL, see
`fonts/OFL.txt`), laid out to the circle.

To see every screen without the watch (Mac, SDL2 from Homebrew):

```bash
pio run -e preview
uv run --no-project --with pillow python preview/render.py   # → preview/out/sheet.png
```

## Speech chime

With *Beeps around speech* on (the default), the device plays a short rising
chime (C6 → G6) before it speaks and the same two tones falling (G6 → C6)
once the answer has played to its end, so you know it's done. A stopped
answer or a replay gets neither.

## Buttons

| | Front (A) | Side (B) |
|---|---|---|
| hold | talk (release sends) | menu (hold again closes) |
| tap / click | stop speaking · flip picture ↔ text | next page · back |
| double-click (on an answer) | | hear the answer again |
| in the agent list | choose | move down |

**Replay.** Double-click the side button on an answer (its text or its picture), or
choose Menu → *Replay answer* (the first row, while there's an answer with sound), to
hear the last answer again, without the cue. The device keeps the whole answer in
memory (its speech, then any sound the agent made, up to ~66 s), so a replay plays
at once and needs no network. If part of the answer never arrived (you stopped it
early, or the link dropped), the device asks Flight Deck to send it again instead
(`{"type":"replay"}`; the speech is synthesized afresh). Powering off forgets it.
A single side click on an answer now waits 0.35 s to make sure it isn't a double-click.

In the side-button list, *Volume* (25/50/75/100%) and *Boost* (off, +6, +10 dB —
extra speech gain with a soft limiter) change with each front-button press and
beep at the new level. Above 75% volume on battery the stick may restart on loud
peaks (M5Stack's note); on USB power it's fine.

Display → *icons* shows an animated character instead of text: Captain, Bot, Bird, Car,
Matrix, Orc, Elf, Dwarf, C64, Amiga, Cat, Dog, Dinosaur, Alien, Caveman, HAL 9000, KITT or
Pac-Man. Menu → *Character »* previews each one cycling through its poses (side button =
next, front = pick, side hold = back).

Menu → *Text size* (small / medium / large) sets the reading text: replies, the working
screen, notices, the menu and the status page (which then spans pages: B = next page, after
the last one back to the main screen). The top bar stays compact.

Hold the front button while switching on to reopen setup. With no Wi-Fi
saved, or none in reach for 30 s, setup opens by itself. (Holding it to wake
from sleep is talking, not setup. See *Talk from sleep*.)

## Setup

1. The device opens a hotspot `Captain-StickS3-xxxx` / `Captain-StopWatch-xxxx` / `Captain-PaperColor-xxxx`
   (scan the QR code on screen).
2. Open http://192.168.4.1 and fill in:
   - one or more Wi-Fi networks (up to 5; it joins whichever is in reach)
   - Flight Deck address: `ws://<mac-ip>:25080` on your network, or `wss://<your deck domain>`
   - access token: Flight Deck → Connections → *Agent access for Claude (MCP)* → Generate token
     (not needed on a deck with auth off)
   - optional: agent slug, spoken-reply language (`en`, `hr`, …), volume, screen-off (5/10/15 s)
     and power-off (15/30/60 s) timeouts; on the StopWatch also buzz and time zone; on the
     PaperColor orientation, status lights, key clicks, reading the menu aloud, power-off
     (1/3/10 min), redrawing while asleep and time zone
3. Save — the device restarts and connects.

The deck needs `SONIOX_API_KEY` (speech in, voice out). A deck reached
through a domain needs that domain in `FD_ALLOWED_HOSTS` or `FD_PUBLIC_URL`.

## Build and flash

```bash
uv tool install platformio
cd firmware/device
pio run -e sticks3 -t upload      # or: -e stopwatch / -e papercolor
```

If the upload can't connect: hold the side button until the green LED
blinks (download mode), upload, then press the side button once to restart.
StopWatch: with USB in, hold the power button ~2 s until the green LED lights.
PaperColor: with USB in, hold the side reset button ~3 s for download mode.
Back up the factory firmware before the first upload:
`esptool read-flash 0 0x1000000 stopwatch-factory.bin` (or `papercolor-factory.bin`).

Serial log: open the port with DTR/RTS released — asserting them resets the
StickS3 into download mode. `pio device monitor --dtr 0 --rts 0`.

## Updates over the air

From 0.8.0 the devices update over Wi-Fi from Flight Deck (design:
`docs/device-ota-plan.md`). Bump `FW_VERSION` in `src/main.cpp`, then:

```bash
uv run --no-project --with cryptography python tools/publish.py sticks3 \
    --deck https://glasses.captain-claw.com --token cc_pat_… --notes "what changed"
```

It builds, signs (ECDSA P-256, key in `~/.captain-claw/firmware-signing/` — back it
up; without it no device accepts a new build) and uploads (admin token). Connected
devices of that kind get the offer at once, others on their next connect:
Menu → *Software update* (or by itself with *Auto update*, when idle on USB). A new
version that can't reach Flight Deck within 90 s rolls back to the previous one.
The key pair was made once with `publish.py keygen`; `src/ota_pubkey.h` is its
public half.

The StickS3 screens use trimmed copies of efontJA (`src/efont_subset.*`, made by
`tools/subset_fonts.py`) — run it again if a screen needs a glyph that isn't kept.

## Back to stock firmware

M5Burner → StickS3 / StopWatch / PaperColor → UIFlow2 (or Xiaozhi), or write back a full flash dump
with `esptool write-flash 0 <dump>.bin` in download mode.
