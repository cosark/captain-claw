// Animated characters for icon mode, drawn with shapes (no image files).
#pragma once

#include <M5Unified.h>

namespace face {

// Stored in settings by number: append new kinds, never reorder.
enum Kind : uint8_t {
  Captain, Bot, Bird, Car, Matrix, Orc, Elf, Dwarf, C64, Amiga,
  Cat, Dog, Dinosaur, Alien, Caveman, Hal, Kitt, PacMan, KindCount
};
enum class Pose : uint8_t { Idle, Listening, Thinking, Speaking, Sad };

const char* name(uint8_t kind);

// The reply being spoken (the C64 types it out while it speaks).
void setText(const String& text);

// Milliseconds between frames that keep this character moving smoothly.
uint32_t frameMs(uint8_t kind);

// Draws the character centred on (cx, cy) in the area under the top bar.
// `level` 0..100 drives the mouth (speaking) or the sound waves (listening);
// `t` is millis() for blinking and motion.
void draw(M5Canvas& c, uint8_t kind, Pose pose, int level, uint32_t t, int cx, int cy);

}  // namespace face
