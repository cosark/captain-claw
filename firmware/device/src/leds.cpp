#include "leds.h"

#include <M5Unified.h>
#include <esp32-hal-rmt.h>
#include <math.h>

#include "board.h"

namespace leds {

// The two lights are WS2812-style LEDs on one data pin (M5Unified's pin
// table: G21 on the PaperColor). M5Unified's own LED driver only works on
// ESP-IDF 5 (its RMT v1 path is empty), so they're driven here with the
// Arduino core's RMT, the way its neopixelWrite() does it: 100 ns ticks,
// 24 bits per LED, green-red-blue.
static rmt_obj_t* rmt = nullptr;

static void write(uint32_t a, uint32_t b) {
  if (!rmt) return;
  rmt_data_t bits[48];
  int i = 0;
  for (uint32_t rgb : {a, b}) {
    uint32_t grb = ((rgb >> 8) & 0xFF) << 16 | ((rgb >> 16) & 0xFF) << 8 | (rgb & 0xFF);
    for (int k = 23; k >= 0; --k, ++i) {
      bool one = (grb >> k) & 1;
      bits[i].level0 = 1;
      bits[i].duration0 = one ? 8 : 4;  // 0.8 / 0.4 µs high
      bits[i].level1 = 0;
      bits[i].duration1 = one ? 4 : 8;  // 0.4 / 0.8 µs low
    }
  }
  rmtWriteBlocking(rmt, bits, 48);
}

static State state = State::Off;
static int level = 0;
static uint8_t maxBright = 48;
static uint32_t flashUntil = 0, alertUntil = 0, actionUntil = 0, lastMs = 0;
static Step lastStep = Step::Narration;
static uint32_t lastColor[2] = {1, 1};  // what the lights show (forces the first write)

void begin() {
  if (!board::kHasLeds) return;
  int pin = M5.getPin(m5::pin_name_t::rgb_led);
  if (pin >= 0 && (rmt = rmtInit(pin, RMT_TX_MODE, RMT_MEM_64)) != nullptr) rmtSetTick(rmt, 100);
  Serial.printf("[claw] status lights on G%d: %s\n", pin, rmt ? "ready" : "no driver");
  off();
}

void setLevel(uint8_t l) { maxBright = l == 0 ? 0 : l == 1 ? 48 : 200; }

void set(State s, int lvl) {
  state = s;
  level = constrain(lvl, 0, 100);
}

void flash() { flashUntil = millis() + 700; }

void alert() { alertUntil = millis() + 900; }

void action(Step step) {
  lastStep = step;
  actionUntil = millis() + 500;
  if (!actionUntil) actionUntil = 1;
}

// A step's colour: light blue a tool, dark blue narration, purple the model.
static void stepColor(Step s, uint8_t* r, uint8_t* g, uint8_t* b) {
  switch (s) {
    case Step::Tool: *r = 40, *g = 190, *b = 255; break;
    case Step::Model: *r = 170, *g = 0, *b = 255; break;
    default: *r = 0, *g = 30, *b = 255; break;
  }
}

static uint32_t scale(uint8_t r, uint8_t g, uint8_t b, float k) {
  k = k < 0 ? 0 : k > 1 ? 1 : k;
  k *= maxBright / 255.0f;
  return ((uint32_t)(r * k) << 16) | ((uint32_t)(g * k) << 8) | (uint32_t)(b * k);
}

static void show(uint32_t a, uint32_t b) {
  if (a == lastColor[0] && b == lastColor[1]) return;
  lastColor[0] = a;
  lastColor[1] = b;
  write(a, b);
}

void off() {
  if (!rmt) return;
  lastColor[0] = lastColor[1] = 1;
  show(0, 0);
}

void tick() {
  if (!rmt) return;
  const uint32_t now = millis();
  if (now - lastMs < 33) return;
  lastMs = now;
  if (maxBright == 0) {
    show(0, 0);
    return;
  }
  if ((int32_t)(flashUntil - now) > 0) {  // white, on/off every 100 ms
    uint32_t c = ((flashUntil - now) / 100) % 2 ? scale(255, 255, 255, 1) : 0;
    show(c, c);
    return;
  }
  if ((int32_t)(alertUntil - now) > 0) {
    uint32_t c = ((alertUntil - now) / 150) % 2 ? scale(255, 0, 0, 1) : 0;
    show(c, c);
    return;
  }
  const float t = now / 1000.0f;
  switch (state) {
    case State::Listening: {  // red, blinking like a REC light; brighter with the voice
      uint32_t c = fmodf(t, 0.8f) < 0.45f ? scale(255, 0, 0, 0.45f + 0.55f * level / 100.0f) : 0;
      show(c, c);
      break;
    }
    case State::Working: {  // a blink per step the deck reports, in its colour; a faint glow between
      uint8_t r, g, b;
      stepColor(lastStep, &r, &g, &b);
      uint32_t c = (int32_t)(actionUntil - now) > 0 ? scale(r, g, b, 1) : scale(r, g, b, 0.06f);
      show(c, c);
      break;
    }
    case State::Speaking: {  // green, blinking with the voice
      uint32_t c = scale(0, 255, 40, level > 8 ? 0.25f + 0.75f * level / 100.0f : 0.04f);
      show(c, c);
      break;
    }
    case State::Connecting: {  // blue, blinking
      uint32_t c = fmodf(t, 1.0f) < 0.5f ? scale(40, 80, 255, 0.8f) : 0;
      show(c, c);
      break;
    }
    case State::Updating: {
      uint32_t c = scale(200, 0, 255, 0.4f + 0.6f * (0.5f + 0.5f * sinf(t * 6.0f)));
      show(c, c);
      break;
    }
    case State::Redrawing: {  // a slow, soft white glow: the paper is changing
      uint32_t c = scale(255, 255, 255, 0.05f + 0.2f * (0.5f + 0.5f * sinf(t * 2.0f)));
      show(c, 0);
      break;
    }
    case State::LowBattery: {  // an orange blip every 5 s
      uint32_t c = fmodf(t, 5.0f) < 0.15f ? scale(255, 80, 0, 1) : 0;
      show(c, 0);
      break;
    }
    default:
      show(0, 0);
      break;
  }
}

}  // namespace leds
