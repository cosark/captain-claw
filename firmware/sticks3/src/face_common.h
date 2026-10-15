// Shared helpers for the character drawings (face*.cpp).
#pragma once

#include "face.h"

namespace face {

constexpr uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

constexpr uint16_t kBlack = rgb(0, 0, 0);
constexpr uint16_t kWhite = rgb(255, 255, 255);
constexpr uint16_t kMouth = rgb(110, 20, 30);
constexpr uint16_t kTongue = rgb(230, 90, 100);
constexpr int kTop = 16;     // the drawing area starts under the top bar
constexpr int kBottom = 135;

bool blinking(uint32_t t);
// Mouth opening in px for a speaking level (with a little jitter so it moves
// like speech rather than a meter).
int openness(int level, int maxOpen, uint32_t t);
uint32_t poseAge(uint32_t t);  // ms since the current pose began
const String& sayText();
float wave(uint32_t t, uint32_t periodMs);  // sine, -1..1

// One drawing per character. Characters listed in ownsPoses() (face.cpp)
// draw their own listening/thinking effects; the rest get the shared waves
// and thought dots.
void drawCaptain(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawBot(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawBird(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawCar(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawCat(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawDog(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawDinosaur(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawOrc(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawElf(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawDwarf(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawCaveman(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawAlien(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawMatrix(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawC64(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawAmiga(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawHal(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawKitt(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);
void drawPacMan(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy);

}  // namespace face
