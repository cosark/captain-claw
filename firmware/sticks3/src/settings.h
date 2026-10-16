// Persistent device settings (NVS): saved Wi-Fi networks, the Flight Deck
// address and token, voice options. Edited from the setup portal.
#pragma once

#include <Arduino.h>

static constexpr int kMaxNetworks = 5;

struct WifiNet {
  String ssid;
  String pass;
};

struct Settings {
  WifiNet nets[kMaxNetworks];
  int netCount = 0;
  String server;        // ws://192.168.1.10:25080  or  wss://deck.example.com
  String token;         // cc_pat_… personal access token ("" on an auth-off deck)
  String agent;         // preferred agent slug ("" = first running)
  String lang;          // voice language for spoken replies ("" = server default)
  bool speak = true;    // read replies aloud
  uint8_t volume = 191; // 0..255 (M5 docs: above ~75% on battery may brown out)
  uint8_t boostDb = 6;  // extra speech gain with a soft limiter: 0, 6 or 10 dB
  uint16_t sleepMin = 15;  // deep sleep after this many idle minutes (0 = never)
  bool femaleVoice = false; // spoken replies: male (Adrian) / female (Emma) on the deck
  bool iconMode = false;    // screen: text stages, or an animated character
  uint8_t character = 0;    // face::Kind (0 = captain)
  bool cueBeep = true;      // chime before speech starts
  bool cueFlash = true;     // flash the screen before speech starts
  uint8_t cueLeadS = 1;     // the cue comes this many seconds (1 or 2) before speech
  uint8_t textSize = 0;     // reading text: 0 small, 1 medium, 2 large

  void load();
  void save() const;
  // Add or update a network (most recent first); false when the list is full.
  bool addNetwork(const String& ssid, const String& pass);
  void removeNetwork(int index);
  bool configured() const { return netCount > 0 && server.length() > 0; }
};

extern Settings settings;

// "stick-a1b2c3" — stable per device (from the Wi-Fi MAC).
String deviceId();
