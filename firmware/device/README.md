# Captain Claw · handheld voice client (M5Stack StickS3 and StopWatch)

Hold the talk button, talk, release. Flight Deck transcribes it, sends it to
one of your agents, and the device shows the reply, reads it aloud, and shows
any picture the agent made.

Server side: `captain_claw/flight_deck/device_bridge.py` (`/device/ws`).

One source tree builds both devices (`src/board.h` holds what differs):

| | StickS3 | StopWatch |
|---|---|---|
| screen | 240×135 LCD (`ui_stick.cpp`) | 466×466 round AMOLED (`ui_round.cpp`) |
| talk button (A) | front | yellow |
| other button (B) | side | the second button |
| extras | — | vibration (buzz before speech, on talk), clock |
| build | `pio run -e sticks3` | `pio run -e stopwatch` |

The sections below say "front"/"side" for A/B.

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

## Buttons

| | Front (A) | Side (B) |
|---|---|---|
| hold | talk (release sends) | menu (hold again closes) |
| tap / click | stop speaking · flip picture ↔ text | next page · back |
| in the agent list | choose | move down |

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
saved, or none in reach for 30 s, setup opens by itself.

## Setup

1. The device opens a hotspot `Captain-StickS3-xxxx` / `Captain-StopWatch-xxxx` (scan the QR code on screen).
2. Open http://192.168.4.1 and fill in:
   - one or more Wi-Fi networks (up to 5; it joins whichever is in reach)
   - Flight Deck address: `ws://<mac-ip>:25080` on your network, or `wss://<your deck domain>`
   - access token: Flight Deck → Connections → *Agent access for Claude (MCP)* → Generate token
     (not needed on a deck with auth off)
   - optional: agent slug, spoken-reply language (`en`, `hr`, …), volume, screen-off (5/10/15 s)
     and power-off (15/30/60 s) timeouts; on the StopWatch also buzz and time zone
3. Save — the device restarts and connects.

The deck needs `SONIOX_API_KEY` (speech in, voice out). A deck reached
through a domain needs that domain in `FD_ALLOWED_HOSTS` or `FD_PUBLIC_URL`.

## Build and flash

```bash
uv tool install platformio
cd firmware/device
pio run -e sticks3 -t upload      # or: -e stopwatch
```

If the upload can't connect: hold the side button until the green LED
blinks (download mode), upload, then press the side button once to restart.
StopWatch: with USB in, hold the power button ~2 s until the green LED lights.
Back up its factory firmware before the first upload:
`esptool read-flash 0 0x1000000 stopwatch-factory.bin`.

Serial log: open the port with DTR/RTS released — asserting them resets the
StickS3 into download mode. `pio device monitor --dtr 0 --rts 0`.

## Back to stock firmware

M5Burner → StickS3 / StopWatch → UIFlow2 (or Xiaozhi), or write back a full flash dump
with `esptool write-flash 0 <dump>.bin` in download mode.
