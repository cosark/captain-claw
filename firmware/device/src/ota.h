// Over-the-air updates (docs/device-ota-plan.md): Flight Deck offers a signed
// build; the device downloads it into the spare app slot, checks its SHA-256
// and ECDSA signature, switches and restarts. A new version runs on probation
// until Wi-Fi and the deck connect — otherwise the bootloader goes back to the
// previous one.
#pragma once

#include <Arduino.h>

namespace ota {

struct Offer {
  String version, sha256, sig, url, notes;
  size_t size = 0;
  bool valid() const { return version.length() && size && sha256.length() == 64 && sig.length() && url.length(); }
};

// a < b → negative, equal → 0, a > b → positive ("0.10.0" > "0.9.3").
int compareVersions(const String& a, const String& b);

// This boot is a freshly installed version that hasn't proven itself yet.
bool onProbation();
// It works (Wi-Fi + deck are up): keep it.
void markGood();
// It doesn't: back to the previous version (restarts).
[[noreturn]] void rollBack();

// Downloads, checks and installs `offer` from `base` (http(s)://host[:port][/prefix]).
// `progress` is called as data arrives. Returns "" when installed (the caller
// restarts), else why not — the running version is untouched then.
String install(const Offer& offer, const String& base, const String& token,
               void (*progress)(size_t done, size_t total));

}  // namespace ota
