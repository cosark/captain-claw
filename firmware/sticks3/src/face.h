// Animated characters for icon mode, drawn with shapes (no image files).
#pragma once

#include <M5Unified.h>

namespace face {

enum Kind : uint8_t { Smiley, Bot, Captain, Bird, KindCount };
enum class Pose : uint8_t { Idle, Listening, Thinking, Speaking, Sad };

const char* name(uint8_t kind);

// Draws the character centred on (cx, cy), about 100 px tall. `level` 0..100
// drives the mouth (speaking) or the sound waves (listening); `t` is millis()
// for blinking and the thinking dots.
void draw(M5Canvas& c, uint8_t kind, Pose pose, int level, uint32_t t, int cx, int cy);

}  // namespace face
