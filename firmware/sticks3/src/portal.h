// Setup portal: the stick opens its own Wi-Fi hotspot with a captive page
// (192.168.4.1) where the user picks Wi-Fi networks and enters the Flight
// Deck address and token. Saving restarts the stick.
#pragma once

#include <Arduino.h>

// Runs until the user saves (then restarts) — never returns.
// `reason` is shown on screen ("No Wi-Fi saved", "Setup", …).
[[noreturn]] void runSetupPortal(const char* reason);
