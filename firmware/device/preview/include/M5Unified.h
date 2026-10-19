// A stand-in for M5Unified in the preview renderers: the "display" is a plain
// sprite (466×466 round, 400×600 paper) the renderer saves to a file, the battery reads what the
// renderer sets. Not used on the devices.
#pragma once

#include <Arduino.h>
#include <M5GFX.h>

namespace m5 {
struct Power_Class {
  enum is_charging_t { is_discharging = 0, is_charging, charge_unknown };
  int level = 82;
  bool charging = false;
  int getBatteryLevel() { return level; }
  int getBatteryVoltage() { return 3900; }
  is_charging_t isCharging() { return charging ? is_charging : is_discharging; }
  void setVibration(uint8_t) {}
};
}  // namespace m5

struct PreviewDisplay : public lgfx::LGFX_Sprite {
  void setBrightness(uint8_t) {}
  void sleep() {}
  void setEpdMode(lgfx::epd_mode_t) {}  // the e-paper's (ui_paper.cpp)
  void setAutoDisplay(bool) {}
};

struct PreviewM5 {
  PreviewDisplay Display;
  m5::Power_Class Power;
};
extern PreviewM5 M5;
