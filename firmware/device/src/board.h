// What differs between the devices this firmware builds for. Everything else
// (the Flight Deck protocol, settings, setup portal, audio, characters) is
// shared; each device has its own screen layer (ui_stick.cpp / ui_round.cpp).
//
//   -DBOARD_STICKS3    M5Stack StickS3: 240×135 LCD, front + side button
//   -DBOARD_STOPWATCH  M5Stack StopWatch: 466×466 round AMOLED, two buttons,
//                      vibration motor
#pragma once

#include <driver/gpio.h>

namespace board {

#if defined(BOARD_STOPWATCH)

constexpr const char* kModel = "StopWatch";   // portal title, hotspot name
constexpr const char* kHw = "stopwatch";      // build kind for over-the-air updates (= PlatformIO env)
constexpr const char* kNoun = "watch";        // "the watch restarts…"
constexpr const char* kIdPrefix = "watch";    // device id: watch-a1b2c3
constexpr const char* kTalkButton = "yellow button";
// M5.BtnA / M5.BtnB, read raw by the 20 ms sampler; the talk button also
// wakes the device from deep sleep (G2 is an RTC GPIO).
constexpr gpio_num_t kTalkPin = GPIO_NUM_2;   // KEYA (yellow)
constexpr gpio_num_t kSidePin = GPIO_NUM_1;   // KEYB
constexpr int kScreenW = 466, kScreenH = 466;
constexpr bool kRound = true;
constexpr bool kHasMotor = true;
constexpr bool kLoudPeakRestarts = false;  // not seen (yet) on the StopWatch
// Pictures bigger than one WebSocket frame arrive in parts (0x03 … 0x02).
constexpr bool kChunkedImages = true;

#else  // BOARD_STICKS3

constexpr const char* kModel = "StickS3";
constexpr const char* kHw = "sticks3";
constexpr const char* kNoun = "stick";
constexpr const char* kIdPrefix = "stick";
constexpr const char* kTalkButton = "front button";
constexpr gpio_num_t kTalkPin = GPIO_NUM_11;  // front (A)
constexpr gpio_num_t kSidePin = GPIO_NUM_12;  // side (B)
constexpr int kScreenW = 240, kScreenH = 135;
constexpr bool kRound = false;
constexpr bool kHasMotor = false;
constexpr bool kLoudPeakRestarts = true;   // M5Stack's note: >75% volume on battery
constexpr bool kChunkedImages = false;

#endif

}  // namespace board
