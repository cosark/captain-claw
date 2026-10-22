// Status lights (PaperColor: two RGB LEDs). The paper takes ~20 s to redraw,
// so the lights say what's happening now: red blinking while it records you
// (like a REC light, brighter when you're louder), a blink for every step
// the deck reports while the agent works — light blue for a tool, dark blue
// for narration and progress, purple for the model thinking — green with the voice
// while it speaks, a soft white glow while the paper redraws. Starting up,
// the left light shows Wi-Fi and the right one the agent; idle, a short blink
// every 10 s says how close sleep is. Devices without lights ignore all of it.
#pragma once

#include <Arduino.h>

namespace leds {

enum class State : uint8_t { Off, Connecting, Listening, Working, Speaking, Redrawing, Updating, LowBattery,
                             Startup, Idle };

void begin();
// 0 off, 1 low, 2 bright (settings.ledLevel).
void setLevel(uint8_t level);
// What the device is doing; `level` 0..100 is the mic or the voice.
void set(State state, int level = 0);
// Idle: a short blink every 10 s — green for the first 30% of the way to
// sleep, blue for the next 50%, yellow for the last 20%.
void setIdle(uint32_t idleMs, uint32_t sleepMs);

// Starting up: both lights yellow; then the left one is Wi-Fi and the right
// one the agent, each green once it's up or red when it can't be reached.
// Both green: they stay so for 6 s, then the start-up show is over.
enum class Link : uint8_t { Trying, Up, Down };
void startup();
void links(Link wifi, Link agent);  // the latest, every loop pass
bool startingUp();

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
