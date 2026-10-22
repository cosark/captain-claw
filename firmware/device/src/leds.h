// Status lights (PaperColor: two RGB LEDs). The paper takes ~20 s to redraw,
// so the lights say what's happening now: red blinking while it records you
// (like a REC light, brighter when you're louder), a blink for every step
// the deck reports while the agent works — light blue for a tool, dark blue
// for narration and progress, purple for the model thinking — green with the voice
// while it speaks, a soft white glow while the paper redraws. Devices
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
// The deck reported a step of the work: one blink, coloured by what it was.
enum class Step : uint8_t { Tool, Narration, Model };
void action(Step step);
// Something went wrong: two red blinks.
void alert();
// From loop(): animates the lights (at most ~30 times a second).
void tick();
// Off now (before sleep).
void off();

}  // namespace leds
