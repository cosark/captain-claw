// Status lights (PaperColor: two RGB LEDs). The paper takes ~20 s to redraw,
// so the lights say what's happening now: red while it listens (brighter
// when you're louder), amber breathing while the agent works, cyan with the
// voice while it speaks, a slow blue glow while the paper redraws. Devices
// without lights ignore all of it.
#pragma once

#include <Arduino.h>

namespace leds {

enum class State : uint8_t { Off, Connecting, Listening, Working, Speaking, Redrawing, Updating, LowBattery };

void begin();
// 0 off, 1 low, 2 bright (settings.ledLevel).
void setLevel(uint8_t level);
// What the device is doing; `level` 0..100 is the mic or the voice.
void set(State state, int level = 0);
// The cue before speech: three quick white blinks.
void flash();
// Something went wrong: two red blinks.
void alert();
// From loop(): animates the lights (at most ~30 times a second).
void tick();
// Off now (before sleep).
void off();

}  // namespace leds
