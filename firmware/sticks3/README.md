# Captain Claw · M5Stack StickS3 voice client

Hold the front button, talk, release. Flight Deck transcribes it, sends it to
one of your agents, and the stick shows the reply, reads it aloud, and shows
any picture the agent made.

Server side: `captain_claw/flight_deck/device_bridge.py` (`/device/ws`).

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

Hold the front button while switching on to reopen setup. With no Wi-Fi
saved, or none in reach for 30 s, setup opens by itself.

## Setup

1. The stick opens a hotspot `Captain-StickS3-xxxx` (scan the QR code on screen).
2. Open http://192.168.4.1 and fill in:
   - one or more Wi-Fi networks (up to 5; it joins whichever is in reach)
   - Flight Deck address: `ws://<mac-ip>:25080` on your network, or `wss://<your deck domain>`
   - access token: Flight Deck → Connections → *Agent access for Claude (MCP)* → Generate token
     (not needed on a deck with auth off)
   - optional: agent slug, spoken-reply language (`en`, `hr`, …), volume, sleep minutes
3. Save — the stick restarts and connects.

The deck needs `SONIOX_API_KEY` (speech in, voice out). A deck reached
through a domain needs that domain in `FD_ALLOWED_HOSTS` or `FD_PUBLIC_URL`.

## Build and flash

```bash
uv tool install platformio
cd firmware/sticks3
pio run -t upload
```

If the upload can't connect: hold the side button until the green LED
blinks (download mode), upload, then press the side button once to restart.

Serial log: open the port with DTR/RTS released — asserting them resets the
StickS3 into download mode. `pio device monitor --dtr 0 --rts 0`.

## Back to stock firmware

M5Burner → StickS3 → UIFlow2 (or Xiaozhi), or write back a full flash dump
with `esptool write-flash 0 <dump>.bin` in download mode.
