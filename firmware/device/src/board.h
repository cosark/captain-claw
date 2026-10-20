// What differs between the devices this firmware builds for. Everything else
// (the Flight Deck protocol, settings, setup portal, audio, characters) is
// shared; each device has its own screen layer (ui_stick.cpp / ui_round.cpp /
// ui_paper.cpp).
//
//   -DBOARD_STICKS3     M5Stack StickS3: 240×135 LCD, front + side button
//   -DBOARD_STOPWATCH   M5Stack StopWatch: 466×466 round AMOLED, two buttons,
//                       vibration motor
//   -DBOARD_PAPERCOLOR  M5Stack PaperColor: 4" Spectra 6 colour e-paper
//                       (400×600, ~20 s to redraw), three buttons, two RGB
//                       LEDs, SHT40 temperature/humidity, RTC
#pragma once

#include <driver/gpio.h>

namespace board {

#if defined(BOARD_STOPWATCH)

constexpr const char* kModel = "StopWatch";   // portal title, hotspot name
constexpr const char* kHw = "stopwatch";      // build kind for over-the-air updates (= PlatformIO env)
constexpr const char* kNoun = "watch";        // "the watch restarts…"
constexpr const char* kIdPrefix = "watch";    // device id: watch-a1b2c3
constexpr const char* kTalkButton = "yellow button";
constexpr const char* kSideButton = "other button";
// M5.BtnA / M5.BtnB, read raw by the 20 ms sampler; the talk button also
// wakes the device from deep sleep (G2 is an RTC GPIO).
constexpr gpio_num_t kTalkPin = GPIO_NUM_2;   // KEYA (yellow)
constexpr gpio_num_t kSidePin = GPIO_NUM_1;   // KEYB
constexpr gpio_num_t kBackPin = GPIO_NUM_NC;  // no third button
constexpr int kScreenW = 466, kScreenH = 466;
constexpr bool kRound = true;
constexpr bool kEpaper = false;
constexpr bool kHasMotor = true;
constexpr bool kHasLeds = false;
constexpr bool kHasClimate = false;
// Voice notes recorded offline are kept (no TF slot: in internal flash) and
// sent when the deck is back (notes.cpp).
constexpr bool kOfflineNotes = true;
constexpr bool kHasSdCard = false;
constexpr bool kLoudPeakRestarts = false;  // not seen (yet) on the StopWatch
// Pictures bigger than one WebSocket frame arrive in parts (0x03 … 0x02).
constexpr bool kChunkedImages = true;

#elif defined(BOARD_PAPERCOLOR)

constexpr const char* kModel = "PaperColor";
constexpr const char* kHw = "papercolor";
constexpr const char* kNoun = "PaperColor";
constexpr const char* kIdPrefix = "paper";
constexpr const char* kTalkButton = "A button";
constexpr const char* kSideButton = "B button";
// M5Unified: BtnA = KEY3 (G10), BtnB = KEY2 (G9), BtnC = KEY1 (G1). All
// three are RTC GPIOs, so any of them wakes the device from deep sleep.
constexpr gpio_num_t kTalkPin = GPIO_NUM_10;  // A: talk / choose
constexpr gpio_num_t kSidePin = GPIO_NUM_9;   // B: next / menu
constexpr gpio_num_t kBackPin = GPIO_NUM_1;   // C: previous / back
constexpr int kScreenW = 400, kScreenH = 600;  // portrait; the menu can turn it
constexpr bool kRound = false;
constexpr bool kEpaper = true;
constexpr bool kHasMotor = false;
constexpr bool kHasLeds = true;
constexpr bool kHasClimate = true;
constexpr bool kOfflineNotes = true;
constexpr bool kHasSdCard = true;   // TF slot (SPI, shared with the panel); flash when there's no card
constexpr bool kLoudPeakRestarts = false;
constexpr bool kChunkedImages = true;

#else  // BOARD_STICKS3

constexpr const char* kModel = "StickS3";
constexpr const char* kHw = "sticks3";
constexpr const char* kNoun = "stick";
constexpr const char* kIdPrefix = "stick";
constexpr const char* kTalkButton = "front button";
constexpr const char* kSideButton = "side button";
constexpr gpio_num_t kTalkPin = GPIO_NUM_11;  // front (A)
constexpr gpio_num_t kSidePin = GPIO_NUM_12;  // side (B)
constexpr gpio_num_t kBackPin = GPIO_NUM_NC;
constexpr int kScreenW = 240, kScreenH = 135;
constexpr bool kRound = false;
constexpr bool kEpaper = false;
constexpr bool kHasMotor = false;
constexpr bool kHasLeds = false;
constexpr bool kHasClimate = false;
constexpr bool kOfflineNotes = false;
constexpr bool kHasSdCard = false;
constexpr bool kLoudPeakRestarts = true;   // M5Stack's note: >75% volume on battery
constexpr bool kChunkedImages = false;

#endif

}  // namespace board
