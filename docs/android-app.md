# Captain Claw HUD for Android

An Android phone app that shows a Flight Deck's HUD (`/hud/`, the frontend
built for smart glasses, see [glasses-hud.md](glasses-hud.md)) full screen.
From the phone you can chat with your running agents, read the markdown files
they wrote, and browse their datastore tables.

The app is a thin native shell: one WebView that loads `<your deck>/hud/` from
the deck itself. The HUD's code is not in the APK, so every Flight Deck deploy
updates the app's HUD too, and the HUD stays same-origin with its deck. It
needs no server changes and no extra auth: the HUD calls `/fd/...` with
relative URLs, opens the chat WebSocket from `window.location`, and keeps its
session in the deck's httpOnly `fd_refresh` cookie, as it does on the glasses.

Source: [`android/hud/`](../android/hud) (Kotlin, Gradle, no Compose).

## Why a WebView shell

| Option | Why not |
|---|---|
| Trusted Web Activity | A TWA ties one APK to one origin (Digital Asset Links). Decks are self-hosted at many origins. |
| HUD bundled into the APK | The HUD would need absolute API URLs, an origin-guard / CORS allowance on every deck, and a bearer refresh token in place of the `fd_refresh` cookie. It would also go stale between app releases. |

Phones get WebView updates from Google Play, so the HUD runs on a current
Chromium.

## Build

Requirements: JDK 17 or newer (Android Studio's bundled JBR works) and the
Android SDK with platform 36.1 (`platforms;android-36.1`). The Gradle wrapper
downloads Gradle 9.5.1 (checksum pinned) and AGP 9.1.1.

```bash
cd android/hud
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"   # macOS; any JDK 17+
echo "sdk.dir=$HOME/Library/Android/sdk" > local.properties                        # or set ANDROID_HOME
./gradlew assembleDebug testDebugUnitTest
```

The APK is `android/hud/app/build/outputs/apk/debug/app-debug.apk`. You can
also open `android/hud` in Android Studio (2025.3 or newer).

A release build needs your own signing key (`signingConfigs` in
`app/build.gradle.kts`); none is committed.

## Install

Distribute the APK directly or through a Google Play **internal testing**
track. Play may reject a public listing: its minimum-functionality policy
turns down apps that only wrap a website in a WebView.

- **USB:** turn on USB debugging on the phone, then
  `adb install -r app/build/outputs/apk/debug/app-debug.apk`.
- **File:** copy the APK to the phone and open it; Android asks you to allow
  installs from that app (the browser, Files) once.

## First run

1. Open **Captain Claw**. It asks for your **Flight Deck address**: the
   address you open Flight Deck at, such as `https://deck.example.com`, or
   `http://192.168.1.20:25080` for a deck on your local network. A path is
   ignored: the deck must be served at the root of its host.
2. Tap **Connect**. The app checks the address with `GET /fd/auth/status` and
   saves it only when Flight Deck answers. Without `http://` or `https://`, it
   tries https first, then http on a local-network address.
3. The HUD opens.

To change the deck later, tap **App settings** on the HUD's agent list (also on
the sign-in screen), long-press the launcher icon → **Deck settings**, or tap
**Deck settings** on the "Can't reach Flight Deck" screen. **Reload HUD** in
the same screen loads the HUD again.

### Plain http only on your local network

Android's network security config can't name IP ranges, so the app allows
clear text in it and enforces the rule itself (`Deck.kt`): `http://` is
accepted only for loopback, private (10/8, 172.16/12, 192.168/16),
link-local, CGNAT / Tailscale (100.64/10) and IPv6 unique-local addresses,
local names (`.local`, `.lan`, `.home.arpa`, `.internal`, single-label names),
and names that resolve to such addresses only. The check runs again whenever
the app opens the HUD. Use `https://` for a deck on the internet.

A self-signed certificate is not accepted: an https deck needs a certificate
the phone trusts.

## Sign in

The app opens on **email and password** sign-in: the phone has a keyboard and
a password manager, so the password field is a real `type=password` field in
a form, and autofill works. **Use a pairing code instead** shows a code to
approve at `<deck>/hud/pair` from a device where you are signed in, as on the
glasses. The approval page labels the request "Captain Claw app (Android)".

The session is the deck's `fd_refresh` cookie. The app accepts cookies and
writes them to disk whenever it goes to the background, so you stay signed in
across launches.

> **`FD_LOCKDOWN` marks the refresh cookie Secure** (as does
> `FD_COOKIE_SECURE=1`). A Secure cookie is only sent over https, so on such a
> deck reached over plain http the sign-in works but doesn't last: the next
> refresh has no cookie and you are back at the sign-in screen. Reach a
> locked-down deck over https.

## Using it

The HUD is the same app as on the glasses, adapted for touch when it detects
the phone (the WebView's user agent ends in `CaptainClawHUD/<version>`):

- Tap rows, tabs and chips. Focus rings are hidden while you use touch; a
  hardware keyboard's arrows or Tab bring them back (the D-pad navigation
  still works).
- **Chat:** tap the field and type or dictate with the keyboard; one tap on
  **Send** sends, and the keyboard stays up. The composer stays above the
  keyboard.
- **Files:** scroll with your finger; a sideways swipe turns a page.
- **Data:** in a record, **‹ Prev** / **Next ›** at the bottom, or a sideways
  swipe, step to the neighbouring record.
- **Read aloud:** Android WebView has no `speechSynthesis`, so the app speaks
  replies with Android's text-to-speech.
- **Back** (gesture or button) goes to the previous HUD screen, and closes the
  app from the agent list.

### Always the deck's current HUD

- Every start of the app loads `/hud/` from the deck, with no-cache headers.
  In the app the HUD doesn't register its offline service worker (and removes
  one an older build registered), so no cached shell is ever shown.
- Coming back to the app checks whether the deck now serves a different HUD
  build and reloads if so (an unsent draft in the composer is lost then).
- If the deck can't be reached, the app shows **Can't reach Flight Deck** with
  **Try again** and **Deck settings**.

## How it works

| File | What |
|---|---|
| `MainActivity.kt` | The WebView: JavaScript and DOM storage on, cookies accepted and flushed in `onPause`, user agent + ` CaptainClawHUD/<version>`. Pages of the deck's origin stay in the app; any other link opens in the browser. Edge-to-edge: the system-bar, cutout and keyboard insets pad the WebView's container (`ViewCompat.setOnApplyWindowInsetsListener`), since `adjustResize` no longer resizes an edge-to-edge window. Back runs `history.back()` while the page's `navigation.canGoBack`, like the Meta Display shell (WebView's own `canGoBack()` skips entries the HUD adds without a tap), else `goBack()`, else finishes. |
| `SettingsActivity.kt` | First-run and settings screen. |
| `Deck.kt` | The saved deck origin, address parsing, the local-network rule, the `/fd/auth/status` check. |
| `Speech.kt` | `speechSynthesis` for the page: a document-start script and a bridge object, both registered with `WebViewCompat` for the deck's origin only (never `addJavascriptInterface`), backed by Android `TextToSpeech`. |

The bridge object, `window.CaptainClawHUD`, also carries `{"t":"settings"}`
from the HUD's **App settings** button.

HUD side (`flight-deck/src/hud`): `device.ts` detects the app (`IS_APP`, host
`android-app`) and any touch-first browser (`IS_TOUCH`); every touch change is
gated on that, so the Meta Display behaves exactly as before.

## Testing

- `./gradlew testDebugUnitTest`: address parsing and the local-network rule.
- The glasses e2e check (`flight-deck/e2e/hud/run.sh`) must still pass.
- On an emulator: run a throwaway deck as that script does (it uses ports
  25190 and 24090), install the APK, and enter `10.0.2.2:25190` (the
  emulator's address for the host's loopback). A debug build enables WebView
  debugging, so `chrome://inspect` (or `adb forward tcp:9333
  localabstract:webview_devtools_remote_<pid>`) reaches the page.
