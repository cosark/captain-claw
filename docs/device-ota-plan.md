# Over-the-air updates for the devices (StickS3, StopWatch)

Status: in progress — branch `feat/device-ota` (2026-10-17).

## Goal

Update the stick and the watch over Wi-Fi from Flight Deck, without a USB
cable, safely: a bad build must never brick a device, and nobody but the
owner of the signing key can put firmware on one.

## Decisions (agreed 2026-10-17)

- **Fonts trimmed** to what the screens draw (Latin, Latin Extended-A,
  punctuation, arrows; katakana for the Matrix rain) so the StickS3 build
  fits two app slots with room to spare.
- **Ask by default**: the device offers the update; *Auto update* (setting)
  installs by itself only while idle and on USB power.
- **Admin-only publishing.**

## How it works

```
Mac: tools/publish.py ──build──sign──upload──▶ Flight Deck (admin)
                                                  │ stores <hw>/<version>.bin + manifest
device ──/device/ws?hw=sticks3&fw=0.8.0──────────▶│
device ◀────── {"type":"update", version, size, sha256, sig, url}
device ──GET /device/firmware/sticks3/0.8.1.bin (device token)──▶
        writes the spare slot · checks SHA-256 + signature · switches · restarts
        new version on probation → marks itself good once Wi-Fi + deck are up
        (crash / reboot / no connection within 90 s → bootloader rolls back)
```

### Partitions

| Device | Table | App slots |
|---|---|---|
| StickS3 (8 MB) | `default_8MB.csv` | 2 × 3.3 MB (`ota_0`, `ota_1`) + otadata |
| StopWatch (16 MB) | `default_16MB.csv` | 2 × 6.25 MB (already) |

NVS stays at `0x9000`, so settings survive the switch. Moving the StickS3 from
`max_app_8MB.csv` back to two slots is the **one last USB flash**.

### Signing

ECDSA P-256 over the SHA-256 of `firmware.bin`. The private key lives on the
Mac only (`~/.captain-claw/firmware-signing/ota-key.pem`, created by
`tools/publish.py keygen`); the public key is compiled into the firmware
(`src/ota_pubkey.h`). The device doesn't verify the deck's TLS certificate
yet, so the signature is what stops a man-in-the-middle from serving firmware.
Hardware Secure Boot (eFuses, irreversible) is deliberately not used.

### Rollback

The Arduino core marks a new app valid on boot unless `verifyRollbackLater()`
returns true; ours does. The bootloader has
`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`. A freshly OTA'd app boots in
`ESP_OTA_IMG_PENDING_VERIFY`; it calls `esp_ota_mark_app_valid_cancel_rollback()`
once Wi-Fi **and** the deck connect, and
`esp_ota_mark_app_invalid_rollback_and_reboot()` if that hasn't happened within
90 s (instead of opening the setup hotspot). A crash or reboot before that
also rolls back. A USB-flashed app is never pending, so nothing changes there.

### Device side

- Reports `hw` (`sticks3` / `stopwatch`) and `fw` on `/device/ws`.
- On `{"type":"update"}`: *Software update* in the menu shows the offer;
  with *Auto update* on it installs when idle and on USB power.
- Install refuses mid-conversation, and below 30 % battery unless charging.
  It closes the WebSocket, downloads over HTTPS with the device token
  (`Authorization: Bearer`), streams into the spare slot (`Update`), hashes as
  it goes, checks SHA-256 and the signature, then `Update.end(true)` and
  restarts. Any failure aborts and leaves the running version untouched.
- After restarting into a new version: a "Updated to x.y.z" notice.

### Flight Deck side (`flight_deck/device_firmware.py`)

- Store: `<fd_home>/device_firmware/<hw>/<version>.bin` + `manifest.json`
  (version, size, sha256, sig, notes, uploaded_at, uploaded_by).
- `POST /fd/device/firmware` — admin (JWT or PAT): multipart hw, version,
  sig, notes, file. Checks the size and SHA-256, keeps the last few versions,
  and offers the new build to connected devices of that kind.
- `GET /fd/device/firmware` — admin: what's published.
- `GET /device/firmware/{hw}/{version}.bin` — device token.
- `/device/ws`: after binding, offer the latest build when it's newer than `fw`.

### Publishing

```
python tools/publish.py keygen                       # once
python tools/publish.py sticks3 --deck https://glasses.captain-claw.com --token cc_pat_… [--notes "…"]
```

Builds the env, reads `FW_VERSION`, hashes, signs, uploads.

## Steps

1. **Firmware (last USB flash)** — trimmed fonts, two slots, OTA client,
   rollback guard, signature check, `hw`/`fw` reporting, menu row,
   "updated" notice. Version 0.8.0.
2. **Flight Deck + publish tool** — store, routes, offer on connect and on
   publish, `tools/publish.py`.
3. **Polish** — Flight Deck *Devices* view (each device, version, last seen),
   release notes on the device.

## Testing

- Unit: firmware store (versions, hashes, admin-only upload, device-token
  download, offer only when newer, matching `hw` only).
- Device: flash 0.8.0 by USB; publish 0.8.1; install from the menu; confirm
  the version on the status page; publish a build that never connects to the
  deck (test flag) and confirm the rollback.
