// Orc, elf, dwarf, caveman and alien.
#include "face_common.h"

namespace face {

// ── orc ─────────────────────────────────────────────────────────────

void drawOrc(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t skin = rgb(100, 150, 60), dark = rgb(55, 95, 30), tusk = rgb(245, 240, 215);
  const uint16_t eye = rgb(250, 200, 40);
  c.fillTriangle(cx - 38, cy - 8, cx - 64, cy - 26, cx - 38, cy + 8, skin);  // ears
  c.fillTriangle(cx + 38, cy - 8, cx + 64, cy - 26, cx + 38, cy + 8, skin);
  c.fillRoundRect(cx - 42, cy - 40, 84, 84, 26, skin);
  int brow = pose == Pose::Thinking ? 3 : (pose == Pose::Sad ? -2 : 0);
  c.fillRoundRect(cx - 36, cy - 26 + brow, 72, 10, 5, dark);              // brow ridge
  for (int s = -1; s <= 1; s += 2) {
    int ex = cx + s * 15, ey = cy - 9;
    if (pose == Pose::Listening || (blinking(t) && pose == Pose::Idle)) {
      c.fillRect(ex - 6, ey - 1, 12, 3, kBlack);                          // squint
    } else {
      c.fillCircle(ex, ey, 5, eye);
      c.fillCircle(ex + (pose == Pose::Thinking ? 2 : 0), ey, 2, kBlack);
    }
  }
  bool flare = pose == Pose::Thinking && (t / 250) % 2;
  c.fillEllipse(cx, cy + 4, 10, 7, dark);                                 // nose
  c.fillCircle(cx - 4, cy + 6, flare ? 3 : 2, kBlack);
  c.fillCircle(cx + 4, cy + 6, flare ? 3 : 2, kBlack);
  int o = pose == Pose::Speaking ? openness(level, 14, t) : 0;
  if (pose == Pose::Sad) {
    c.fillArc(cx, cy + 38, 16, 19, 220, 320, dark);
  } else {
    c.fillRoundRect(cx - 22, cy + 16, 44, 5 + o, 3, kMouth);
    c.fillRect(cx - 24, cy + 21 + o, 48, 4, dark);                         // lower lip
  }
  // Tusks ride on the lower jaw.
  int ty = cy + 23 + (pose == Pose::Sad ? 4 : o);
  c.fillTriangle(cx - 20, ty, cx - 15, ty - 15, cx - 10, ty, tusk);
  c.fillTriangle(cx + 10, ty, cx + 15, ty - 15, cx + 20, ty, tusk);
}

// ── elf ─────────────────────────────────────────────────────────────

void drawElf(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t skin = rgb(250, 222, 196), hair = rgb(240, 215, 120), hairDark = rgb(190, 160, 70);
  const uint16_t eye = rgb(60, 170, 90);
  c.fillRoundRect(cx - 40, cy - 46, 80, 102, 30, hair);                  // long hair
  // Ears: twitch while listening, droop when sad.
  int tipY = pose == Pose::Sad ? cy - 4 : cy - 38;
  if (pose == Pose::Listening) tipY += (int)(wave(t, 260) * 4) - 4;
  c.fillTriangle(cx - 28, cy - 10, cx - 28, cy + 6, cx - 66, tipY, skin);
  c.fillTriangle(cx + 28, cy - 10, cx + 28, cy + 6, cx + 66, tipY, skin);
  c.fillEllipse(cx, cy + 2, 30, 40, skin);
  c.fillEllipse(cx, cy - 32, 32, 13, hair);                               // fringe
  c.drawFastHLine(cx - 26, cy - 22, 52, rgb(230, 195, 60));               // circlet
  c.fillCircle(cx, cy - 22, 3, rgb(80, 200, 255));
  int dx = pose == Pose::Thinking ? 3 : 0, dy = pose == Pose::Thinking ? -2 : 0;
  for (int s = -1; s <= 1; s += 2) {
    int ex = cx + s * 12, ey = cy - 4;
    c.drawLine(ex - 6, ey - 9 + (pose == Pose::Sad ? 2 * s : 0), ex + 6, ey - 10, hairDark);
    if (blinking(t) && pose != Pose::Thinking) {
      c.fillRect(ex - 6, ey, 12, 2, kBlack);
    } else {
      c.fillEllipse(ex, ey, 6, 4, kWhite);
      c.fillCircle(ex + dx, ey + dy, 3, eye);
      c.fillCircle(ex + dx, ey + dy, 1, kBlack);
    }
  }
  c.drawLine(cx, cy + 2, cx - 3, cy + 10, rgb(220, 170, 150));            // nose
  if (pose == Pose::Speaking) {
    int o = openness(level, 10, t);
    c.fillEllipse(cx, cy + 20, 6, o / 2 + 1, kMouth);
  } else if (pose == Pose::Sad) {
    c.fillArc(cx, cy + 26, 6, 8, 220, 320, rgb(190, 90, 90));
  } else {
    c.fillArc(cx, cy + 12, 7, 9, 40, 140, rgb(190, 90, 90));
  }
}

// ── dwarf ───────────────────────────────────────────────────────────

void drawDwarf(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t skin = rgb(240, 190, 160), beard = rgb(200, 90, 40), beardDark = rgb(150, 60, 25);
  const uint16_t steel = rgb(160, 165, 175), steelDark = rgb(100, 105, 115), gold = rgb(240, 190, 40);
  int bounce = pose == Pose::Speaking ? level / 12 : (pose == Pose::Sad ? 4 : 0);
  c.fillCircle(cx, cy - 4, 30, skin);
  c.fillEllipse(cx, cy + 18 + bounce, 38, 26, beard);                     // beard
  for (int s = -1; s <= 1; s += 2) {                                      // braids
    c.fillRoundRect(cx + s * 18 - 4, cy + 36 + bounce, 8, 14, 3, beardDark);
    c.fillRect(cx + s * 18 - 5, cy + 42 + bounce, 10, 3, gold);
  }
  int o = pose == Pose::Speaking ? openness(level, 10, t) : 0;
  c.fillEllipse(cx, cy + 15 + bounce / 2, 7, o / 2 + 1, kMouth);
  c.fillEllipse(cx - 11, cy + 8, 12, 5, beardDark);                        // moustache
  c.fillEllipse(cx + 11, cy + 8, 12, 5, beardDark);
  c.fillCircle(cx, cy + 1, 7, rgb(225, 150, 130));                         // nose
  for (int s = -1; s <= 1; s += 2) {
    int ex = cx + s * 11;
    int browY = pose == Pose::Sad ? 2 : (pose == Pose::Thinking ? -1 : 0);
    c.fillRoundRect(ex - 7, cy - 17 + browY - (pose == Pose::Sad ? s : 0), 14, 5, 2, beardDark);
    if (blinking(t) && pose == Pose::Idle) c.fillRect(ex - 3, cy - 9, 7, 2, kBlack);
    else c.fillCircle(ex + (pose == Pose::Thinking ? 2 : 0), cy - 9, 3, kBlack);
  }
  // Helmet, tilting while listening.
  int hx = cx + (pose == Pose::Listening ? (int)(wave(t, 700) * 3) : 0);
  c.fillArc(hx, cy - 14, 0, 33, 180, 360, steel);
  c.fillRect(hx - 34, cy - 17, 68, 6, steelDark);
  c.fillRect(hx - 3, cy - 17, 6, 14, steelDark);                          // nose guard
  for (int i = -2; i <= 2; ++i) c.fillCircle(hx + i * 12, cy - 14, 1, kWhite);  // rivets
  if (pose == Pose::Thinking) {                                           // stroking the beard
    int hy = cy + 26 + (int)(wave(t, 600) * 6);
    c.fillCircle(cx + 24, hy, 8, skin);
    c.fillRoundRect(cx + 16, hy - 2, 8, 4, 2, skin);
  }
}

// ── caveman ─────────────────────────────────────────────────────────

void drawCaveman(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t skin = rgb(205, 150, 105), hair = rgb(60, 40, 25), wood = rgb(140, 90, 45);
  const uint16_t woodDark = rgb(95, 60, 30), stubble = rgb(110, 75, 50);
  // Club over the shoulder — on the ground when the agent is away.
  if (pose == Pose::Sad) {
    c.drawWideLine(cx + 26, cy + 52, cx + 86, cy + 48, 3, wood);
    c.fillCircle(cx + 92, cy + 46, 10, wood);
  } else {
    c.drawWideLine(cx + 28, cy + 44, cx + 60, cy - 22, 3.5f, wood);
    c.fillCircle(cx + 62, cy - 30, 13, wood);
    c.fillCircle(cx + 58, cy - 34, 3, woodDark);
    c.fillCircle(cx + 67, cy - 26, 2, woodDark);
  }
  for (int i = 0; i < 7; ++i) {                                          // wild hair
    int bx = cx - 40 + i * 13;
    c.fillTriangle(bx, cy - 26, bx + 13, cy - 26, bx - 4 + (i % 3) * 5, cy - 52 + (i % 2) * 6, hair);
  }
  c.fillCircle(cx - 34, cy + 2, 7, skin);                                 // ears
  c.fillCircle(cx + 34, cy + 2, 7, skin);
  c.fillRoundRect(cx - 34, cy - 30, 68, 76, 24, skin);
  c.fillEllipse(cx, cy - 28, 38, 12, hair);
  int browY = pose == Pose::Thinking ? 2 : (pose == Pose::Sad ? 3 : 0);
  c.fillRoundRect(cx - 26, cy - 15 + browY, 52, 7, 3, hair);              // unibrow
  if (blinking(t) && pose == Pose::Idle) {
    c.fillRect(cx - 15, cy - 4, 7, 2, kBlack);
    c.fillRect(cx + 8, cy - 4, 7, 2, kBlack);
  } else {
    c.fillCircle(cx - 12, cy - 4, 3, kBlack);
    c.fillCircle(cx + 12, cy - 4, 3, kBlack);
  }
  c.fillEllipse(cx, cy + 8, 10, 8, rgb(185, 125, 85));                    // nose
  c.fillEllipse(cx, cy + 30, 30, 14, stubble);
  int o = pose == Pose::Speaking ? openness(level, 14, t) : 0;
  if (pose == Pose::Sad) {
    c.fillArc(cx, cy + 40, 10, 13, 220, 320, kMouth);
  } else {
    c.fillRoundRect(cx - 12, cy + 22, 24, 4 + o, 3, kMouth);
    if (o > 4) c.fillRect(cx - 8, cy + 22, 16, 3, rgb(240, 235, 210));   // teeth
  }
  if (pose == Pose::Thinking) {                                          // scratching head
    c.fillCircle(cx - 38, cy - 26 + (int)(wave(t, 240) * 4), 10, skin);
  } else if (pose == Pose::Listening) {                                  // hand to ear
    c.fillEllipse(cx - 44, cy + 2, 8, 13, skin);
  }
}

// ── alien ───────────────────────────────────────────────────────────

void drawAlien(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t skin = rgb(165, 190, 165), skinDark = rgb(120, 145, 120);
  int vib = pose == Pose::Listening ? (int)(wave(t, 90) * 3) : 0;
  uint8_t g = pose == Pose::Speaking ? 90 + level * 165 / 100 : 90;
  uint16_t glow = rgb(40, g, 60);
  c.drawWideLine(cx - 14, cy - 36, cx - 26 + vib, cy - 52, 1.5f, skinDark);  // antennae
  c.drawWideLine(cx + 14, cy - 36, cx + 26 - vib, cy - 52, 1.5f, skinDark);
  c.fillCircle(cx - 26 + vib, cy - 53, 4, glow);
  c.fillCircle(cx + 26 - vib, cy - 53, 4, glow);
  c.fillEllipse(cx, cy - 10, 40, 34, skin);                               // head
  c.fillEllipse(cx, cy + 18, 26, 24, skin);
  c.fillTriangle(cx - 24, cy + 20, cx + 24, cy + 20, cx, cy + 46, skin);  // chin
  for (int s = -1; s <= 1; s += 2) {                                      // big slanted eyes
    int ex = cx + s * 18, ey = cy + 2;
    c.fillEllipse(ex, ey, 14, 9, kBlack);
    c.fillTriangle(cx + s * 32, ey - 1, cx + s * 26, ey - 9, cx + s * 22, ey + 6, kBlack);
    if (pose == Pose::Speaking) c.fillEllipse(ex, ey, 8, 5, glow);       // eyes glow with the voice
    else c.fillCircle(ex - 4 * s, ey - 3, 2, rgb(200, 210, 230));          // reflection
    if (pose == Pose::Sad || (blinking(t) && pose == Pose::Idle)) c.fillRect(ex - 15, ey - 10, 30, pose == Pose::Sad ? 9 : 19, skin);
  }
  c.drawFastHLine(cx - 4, cy + 30, 8, skinDark);                          // tiny mouth
  if (pose == Pose::Thinking) {                                          // a saucer beaming
    int ux = cx + 60, uy = cy - 40;
    bool on = (t / 200) % 2;
    for (int i = 0; i < 4; ++i) {
      if ((i + (t / 200)) % 2) c.fillTriangle(ux - 3 - i * 4, uy + 4 + i * 12, ux + 3 + i * 4, uy + 4 + i * 12, ux, uy + 4, rgb(90, 255, 140));
    }
    c.fillEllipse(ux, uy, 16, 5, rgb(170, 175, 190));
    c.fillCircle(ux, uy - 4, 6, rgb(120, 200, 255));
    c.fillCircle(ux - 8, uy + 1, 1, on ? rgb(255, 80, 80) : rgb(255, 230, 80));
    c.fillCircle(ux + 8, uy + 1, 1, on ? rgb(255, 230, 80) : rgb(255, 80, 80));
  }
}

}  // namespace face
