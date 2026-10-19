#include "leds.h"

#include <M5Unified.h>
#include <math.h>

#include "board.h"

namespace leds {

static State state = State::Off;
static int level = 0;
static uint8_t maxBright = 48;
static uint32_t flashUntil = 0, alertUntil = 0, lastMs = 0;
static uint32_t lastColor[2] = {1, 1};  // what the lights show (forces the first write)

void begin() {
  if (!board::kHasLeds || !M5.Led.isEnabled()) return;
  M5.Led.setAutoDisplay(false);
  M5.Led.setBrightness(255);  // the colours carry the level (setLevel)
  off();
}

void setLevel(uint8_t l) { maxBright = l == 0 ? 0 : l == 1 ? 48 : 200; }

void set(State s, int lvl) {
  state = s;
  level = constrain(lvl, 0, 100);
}

void flash() { flashUntil = millis() + 700; }

void alert() { alertUntil = millis() + 900; }

static uint32_t scale(uint8_t r, uint8_t g, uint8_t b, float k) {
  k = k < 0 ? 0 : k > 1 ? 1 : k;
  k *= maxBright / 255.0f;
  return ((uint32_t)(r * k) << 16) | ((uint32_t)(g * k) << 8) | (uint32_t)(b * k);
}

static void show(uint32_t a, uint32_t b) {
  if (a == lastColor[0] && b == lastColor[1]) return;
  lastColor[0] = a;
  lastColor[1] = b;
  M5.Led.setColor(0, (a >> 16) & 255, (a >> 8) & 255, a & 255);
  M5.Led.setColor(1, (b >> 16) & 255, (b >> 8) & 255, b & 255);
  M5.Led.display();
}

void off() {
  if (!board::kHasLeds || !M5.Led.isEnabled()) return;
  lastColor[0] = lastColor[1] = 1;
  show(0, 0);
}

void tick() {
  if (!board::kHasLeds || !M5.Led.isEnabled()) return;
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
    case State::Listening: {  // red, brighter with the voice
      uint32_t c = scale(255, 0, 0, 0.35f + 0.65f * level / 100.0f);
      show(c, c);
      break;
    }
    case State::Working: {  // amber, the two breathing in turn
      float a = 0.5f + 0.5f * sinf(t * 3.9f);
      show(scale(255, 120, 0, 0.15f + 0.85f * a), scale(255, 120, 0, 0.15f + 0.85f * (1 - a)));
      break;
    }
    case State::Speaking: {
      uint32_t c = scale(0, 200, 255, 0.3f + 0.7f * level / 100.0f);
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
    case State::Redrawing: {  // a slow, soft blue glow: the paper is changing
      uint32_t c = scale(60, 90, 255, 0.08f + 0.3f * (0.5f + 0.5f * sinf(t * 2.0f)));
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
