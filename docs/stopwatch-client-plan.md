# M5Stack StopWatch client — plan

Goal: the M5Stack StopWatch (C152) becomes a second Captain Claw push-to-talk
device with everything the StickS3 client does today (fw 0.6.4). It talks to
the same Flight Deck `/device/ws`, and one firmware tree builds both devices.

## 1. Hardware: StickS3 vs StopWatch

| | StickS3 (today) | StopWatch (C152) | Impact |
|---|---|---|---|
| SoC | ESP32-S3-PICO-1-N8R8 | ESP32-S3R8 | same toolchain |
| Flash / PSRAM | 8 MB / 8 MB OPI | **16 MB** / 8 MB OPI | room for OTA and big fonts |
| Display | 240×135 LCD, landscape | **466×466 round AMOLED** (CO5300, QSPI), 1.75" | new UI layout (main work) |
| Touch | — | **CST820** (I2C 0x15, INT G13) | optional extra input |
| Audio | ES8311, mic + speaker | ES8311 + AW8737A amp, 1 W speaker, MEMS mic | `M5.Speaker`/`M5.Mic` already handle it |
| Buttons | front A = G11, side B = G12 | **KEYA (yellow) = G2, KEYB (blue) = G1**, plus a power button on the PMIC | pins in two places in `main.cpp` |
| Extras | — | **vibration motor**, BMI270 IMU, RX8130CE RTC | haptics, raise-to-wake, clock face |
| Power | M5PM1, small cell | M5PM1, **450 mAh** | same API |
| Grove | yes | HY2.0-4P (G10/G11) + 2.54 headers | not used |

**Library support already exists.** M5Unified 0.2.26 / M5GFX auto-detect `board_M5StopWatch`:
display (`Panel_StopWatch` on CO5300), touch, buttons (`BtnA` = G2, `BtnB` = G1),
speaker on I2S0 and mic on I2S1 (shared clock pins, so still **one at a time**, as on the
StickS3), M5IOE1 expander (codec power, PA enable, motor PWM), `M5.Power.setVibration()`,
battery and charging, and the RTC. Support is new: ES8311 capture fixes landed between
2026-08-31 and 2026-09-02, and a GPIO46/QSPI fix on 2026-10-02. **Pin M5Unified and M5GFX to
tags** rather than tracking `master` as the StickS3 does today.

M5Stack's PlatformIO environment: `board = esp32s3box`, `espressif32@6.12.0`,
`default_16MB.csv`, `memory_type = qio_opi`, and the same USB CDC flags as ours. With OPI-PSRAM
off, M5GFX falls back to direct drawing, which renders wrongly at odd coordinates. Keep PSRAM on.

## 2. Code layout: one tree, two envs

Almost everything in `firmware/sticks3` carries over unchanged: the protocol and state machine
(`main.cpp`), `settings`, `portal`, `audio` (through M5Unified) and the 18 characters. The
display is the only real difference, and `ui.h` is already the seam between the logic and
the screen.

- `git mv firmware/sticks3 firmware/device`, with `[env:sticks3]` (the default, so the StickS3
  flow stays as it is) and `[env:stopwatch]`.
- New `board.h`, with values chosen by `-DBOARD_STICKS3` or `-DBOARD_STOPWATCH`:
  - talk and side button GPIOs: the 20 ms sampler (`sampleButtons`) and the deep-sleep ext0
    wake pin. These are the only raw pins in `main.cpp`.
  - screen size sent as `sw`/`sh`, plus a new `round=1`
  - hotspot name `Captain-StickS3-xxxx` or `Captain-StopWatch-xxxx`
  - device id prefix `stick-` or `watch-`
  - device name in the portal and in on-screen text ("Stick ready…" becomes "Watch ready…")
- `ui_stick.cpp` (today's `ui.cpp`, renamed) and a new `ui_round.cpp`. Both implement the same
  `ui.h`; `build_src_filter` picks one per env.
- The StopWatch env uses `default_16MB.csv` (two ~6.25 MB app slots), so OTA becomes possible
  later. The StickS3 keeps `max_app_8MB.csv`.

Alternative: copy the tree to `firmware/stopwatch`. It's faster on day one, but every protocol
fix then has to land twice. I don't recommend it.

## 3. Feature parity map

| StickS3 feature | StopWatch |
|---|---|
| Hold front = talk, release = send; tap = stop speaking / flip picture↔text | Hold **yellow (A)**, same gestures |
| Side click = next page / next row; side hold = menu | **Blue (B)**, same gestures |
| Hold A at power-on, or no Wi-Fi for 30 s = setup hotspot with QR, up to 5 networks, deck + token + agent + lang + volume + timeouts | Same portal; QR centred on the round screen |
| Top bar: Wi-Fi/agent health blocks, agent name, state, battery % colour bands | **Edge ring + top arc** (see §4) |
| Working screen: what it heard, latest activity lines, status line, "12s · 3 steps" | Same content in the round text area; the timer in big stopwatch digits; steps as ticks on the ring |
| Reply text, paged, small/medium/large | Chord-aware wrapping (§4); page dots on the bottom arc |
| Spoken reply, volume 25–100 %, boost 0/+6/+10 dB | Same; check the volume steps on the 1 W speaker |
| Speech cue: chime and/or flash, 1 or 2 s lead | Same, plus **buzz** (motor); the flash becomes a ring flash (a white full screen is harsh on AMOLED) |
| Pictures (JPEG), flip picture↔text | Up to 466×466, fitted to the circle (§5) |
| Icon mode, 18 characters, carousel picker | Same characters, scaled up (§4) |
| Agent picker, Status page (paged), RTT ping | Wheel picker (§4); same rows |
| Male/female voice, speak on/off | Unchanged (server prefs) |
| Screen-off 5/10/15 s (dims 2 s before), power-off 15/30/60 s, dark-screen press only wakes (except talk) | Same; deep sleep wakes on KEYA (G2 is an RTC GPIO, so ext0 works) |
| Proactive replies, errors, notices | Unchanged |

## 4. Round UI (`ui_round.cpp`)

The screen is 466×466 and drawn the same way as today: a full-screen `M5Canvas` in PSRAM
(466×466×2 ≈ 434 KB), pushed in one go, with no partial pushes (CO5300 needs even
coordinates). Expected push time is ~25–40 ms, so the animated states cap at ~20–25 fps.

- **Edge ring (~8 px), the round-screen replacement for the top bar's state colour:**
  - idle: off. AMOLED black costs nothing and avoids burn-in. Two small dots at 10 and 2
    o'clock show Wi-Fi and agent health.
  - listening: red; its thickness follows the mic level, and an arc fills toward the 110 s
    recording cap.
  - working: a yellow arc travels round the ring; each tool step adds a tick on the bezel.
  - speaking: cyan, pulsing with `audio::outputLevel()`.
  - offline: red dashes.
- **Top arc:** agent name and state, centred at y ≈ 40 (about 260 px of chord).
  **Bottom arc:** battery % in its colour band, and page dots.
- **Text area, chord-aware wrapping:** each line's width is the circle's chord at that line's
  y, minus a margin. This fits far more text than the inscribed 330×330 square. `setReply`,
  `showWorking` and `showPanel` all use the same wrapper.
- **Fonts:** at ~266 ppi, efontJA tops out too small (24 px). Proposed sizes: small = efontJA_24,
  medium/large = **smooth VLW fonts** (Latin + Latin Extended-A, about 30 and 38 px), loaded from
  flash. Antialiasing looks good on AMOLED, and these fonts include **Đ** (efontJA lacks it).
  Matrix keeps efontJA for katakana. This needs a little room, which 16 MB gives.
- **Characters:** draw them into today's 240×119 face sprite unchanged, then
  `pushRotateZoomWithAA` about 1.7× onto the canvas. All 18 characters work on day one with no
  rewrites. If a character looks soft, give that one a native scale factor later (the
  `kTop`/`kBottom` constants in `face_common.h` are the only hard-coded geometry).
- **Menu and agent picker:** a vertical "wheel". The selected row is centred and large; its
  neighbours are smaller and dimmer above and below. B moves to the next row and A chooses,
  exactly as today.
- **Brightness and burn-in:** AMOLED `setBrightness` works as today. Static bright elements only
  last until screen-off (≤15 s), so there's no extra burn-in work.

## 5. Flight Deck changes (`device_bridge.py`)

Small and back-compatible. The StickS3 never asks for either change, so it is unaffected.

1. **`round=1`:** `fit_jpeg` fits the picture into the largest rectangle of its own aspect
   ratio that fits inside the circle (w = D·r/√(1+r²), h = D/√(1+r²)), on black. Without it,
   a letterboxed 466×466 picture loses its corners.
2. **Chunked pictures (`img=chunks`):** 466×466 in ≤14 KB (the WebSocket library's frame cap)
   means quality 20–30 JPEGs. With `img=chunks`, the server sends `0x03` + ≤14 KB parts and then
   `0x02` + the last part; the device appends the parts in PSRAM. Raise the cap to about 120 KB
   for these devices (`max_bytes` from the query). The `image` JSON already carries `size`.
3. Update the module docstring (it says "M5Stack StickS3"), and keep the `"stick"` fallback
   device id.
4. Tests: a new `tests/test_device_bridge.py` covering the circle fit (aspect cases), the chunk
   split/sequence, and the `sw`/`sh`/`round` parsing. No device test file exists yet.

Nothing changes in auth (PAT), STT/TTS, the glasses bus or the surface rules. The
`device` surface already asks for short replies.

## 6. StopWatch extras (after parity, each one optional)

- **Haptics:** a buzz on talk start/stop and before speech (a new cue option), and a long buzz
  on errors. Menu: *Buzz before speech*, *Buzz on talk*. NVS keys `cueBuzz` and `talkBuzz`.
- **Touch:** tap a reply = next page, swipe = scroll, tap a picture = flip, tap a menu row =
  choose, hold the screen = talk (a second talk gesture). The buttons keep working identically.
- **Raise-to-wake:** BMI270 wrist-tilt wakes the screen (IMU_INT goes through the M5IOE1, so
  check whether it can wake the chip from deep sleep or only from light sleep).
- **Clock face when idle:** NTP → RX8130 RTC, analog/digital time on the idle screen, so it
  works as a watch. Uses the screen-off timer like everything else.
- **OTA:** the 16 MB layout has two app slots; add a deck-served firmware URL later.

## 7. Bring-up checklist (the day it arrives)

1. **Back up the factory image first:** `esptool read-flash 0 0x1000000 stopwatch-factory.bin`
   in download mode (hold the power button about 2 s with USB in, until the green LED). Keep it
   next to `~/Dev/m5sticks3-backup/`.
2. Check the hardware version: on v1.0 boards, the 2.54 header pin labelled "BAT" is actually
   **5V IN**. Never connect a battery to it.
3. Flash a bring-up build with the board autodetect log, buttons, display, touch, mic level,
   speaker tone and vibration. Note which physical edge the buttons sit on and set the
   rotation so the talk button is natural.
4. **Mic first-syllable test:** the ES8311 analog stage warms up for about 1 s after power-up.
   If the start of a push-to-talk is lost, keep the ADC powered while the screen is on, or keep
   a short pre-roll buffer.
5. Volume steps at 25/50/75/100 % on battery: does it brown out on peaks like the StickS3?
6. Deep sleep: KEYA wake works, and sleep current is acceptable with the AMOLED, codec, PA and
   motor off. If it's too high, use `M5.Power.powerOff()` for the long timeout (the power button
   then wakes it).
7. Serial: assume the StickS3 behaviour (opening the port can reset the device) until proven
   otherwise. Check the connection in the FD log instead.

## 8. Phases

| Phase | What | Needs the device? |
|---|---|---|
| 0 | Shared tree + `board.h` + `[env:stopwatch]` compiling; StickS3 build unchanged and re-flashed to confirm | No |
| 1 | `ui_round.cpp`: ring, arcs, chord wrap, fonts, wheel menu, scaled characters, status | Layout yes; code can start before |
| 2 | Flight Deck: `round=1` fit + chunked pictures + tests | No |
| 3 | Bring-up checklist (§7), tune rotation, volume, mic, sleep | Yes |
| 4 | Extras (§6): haptics first, then touch, clock face, raise-to-wake, OTA | Yes |

Phases 0 and 2 can land before the StopWatch arrives. Phase 1 can be drafted against
the 466×466 geometry and tuned on the device.

Deploy: the server part = pull + FD restart (no agent restarts). Firmware =
`pio run -e stopwatch -t upload`.

Sources: [M5Stack StopWatch docs](https://docs.m5stack.com/en/core/StopWatch),
[CNX Software write-up](https://www.cnx-software.com/2026/05/22/m5stack-stopwatch-esp32-s3-devkit-offers-1-75-inch-touch-amoled-microphone-speaker-and-gpio-expansion/),
[M5Unified source](https://github.com/m5stack/M5Unified), [M5GFX source](https://github.com/m5stack/M5GFX).
