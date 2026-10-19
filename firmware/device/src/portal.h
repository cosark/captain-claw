// Setup portal: the stick opens its own Wi-Fi hotspot with a captive page
// (192.168.4.1) where the user picks Wi-Fi networks and enters the Flight
// Deck address and token. Saving restarts the stick.
#pragma once

#include <Arduino.h>

// Runs until the user saves (then restarts) — never returns.
// `reason` is shown on screen ("No Wi-Fi saved", "Setup", …).
[[noreturn]] void runSetupPortal(const char* reason);

// Settings on a phone while the device is on Wi-Fi: the same page as the
// hotspot's, served on the device's own address. Only the link the screen
// shows opens it — it carries a one-time key. Returns that link ("" when
// not on Wi-Fi). Saving restarts the device, as in setup.
String phoneSettingsBegin();
void phoneSettingsPoll();  // from loop() while the link is on screen
void phoneSettingsEnd();
bool phoneSettingsActive();
