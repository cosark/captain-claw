// Car, cat, dog and dinosaur.
#include "face_common.h"

namespace face {

// ── car (front view, eyes in the windscreen, grille for a mouth) ─────

void drawCar(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t paint = rgb(215, 40, 50), dark = rgb(140, 20, 30), glass = rgb(150, 210, 255);
  const uint16_t tyre = rgb(25, 25, 25), chrome = rgb(190, 195, 200), lamp = rgb(255, 235, 120);
  int y = cy + (pose == Pose::Sad ? 4 : 0);  // sags when the agent is away
  c.fillRoundRect(cx - 58, y + 20, 22, 26, 5, tyre);
  c.fillRoundRect(cx + 36, y + 20, 22, 26, 5, tyre);
  c.fillRoundRect(cx - 46, y - 50, 92, 40, 14, paint);   // roof
  c.fillRoundRect(cx - 39, y - 44, 78, 28, 9, glass);    // windscreen
  c.fillRoundRect(cx - 64, y - 18, 128, 46, 14, paint);  // body
  c.drawFastHLine(cx - 58, y - 12, 116, dark);
  // Eyes in the windscreen.
  int ey = y - 30, dx = pose == Pose::Thinking ? 3 : 0, dy = pose == Pose::Thinking ? -3 : 0;
  for (int s = -1; s <= 1; s += 2) {
    int ex = cx + s * 17;
    c.fillEllipse(ex, ey, 11, 10, kWhite);
    c.fillCircle(ex + dx, ey + 1 + dy, 5, kBlack);
    if (blinking(t) && pose != Pose::Thinking) c.fillRect(ex - 12, ey - 11, 24, 22, paint);
    else if (pose == Pose::Sad) c.fillRect(ex - 12, ey - 11, 24, 11, paint);  // heavy lids
  }
  // Headlights, and hazard blinkers while thinking.
  for (int s = -1; s <= 1; s += 2) {
    c.fillCircle(cx + s * 46, y - 1, 10, chrome);
    c.fillCircle(cx + s * 46, y - 1, 7, lamp);
    bool on = pose == Pose::Thinking && (t / 400) % 2;
    c.fillRoundRect(cx + s * 57 - 5, y + 8, 10, 6, 2, on ? rgb(255, 170, 0) : rgb(120, 70, 0));
  }
  // Grille: bars bounce with the voice.
  c.fillRoundRect(cx - 28, y - 6, 56, 22, 6, rgb(30, 30, 35));
  for (int i = 0; i < 6; ++i) {
    int h = 14;
    if (pose == Pose::Speaking) {
      h = level > 5 ? constrain(level * 18 / 100 + (int)((t / 60 + i * 41) % 7) - 3, 3, 18) : 3;
    } else if (pose == Pose::Sad) {
      h = 4;
    }
    c.fillRect(cx - 24 + i * 9, y + 5 - h / 2, 5, h, chrome);
  }
  c.fillRoundRect(cx - 60, y + 20, 120, 8, 4, chrome);   // bumper
  c.fillRect(cx - 13, y + 21, 26, 6, kWhite);            // plate
}

// ── cat ─────────────────────────────────────────────────────────────

void drawCat(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t fur = rgb(240, 150, 60), furDark = rgb(190, 100, 35), pink = rgb(250, 170, 180);
  const uint16_t eye = rgb(140, 220, 90), whisker = rgb(250, 240, 230);
  // Tail behind the head: sways, swishes while thinking.
  float sway = wave(t, pose == Pose::Thinking ? 600 : 2400) * (pose == Pose::Thinking ? 14 : 5);
  c.drawWideLine(cx + 30, cy + 34, cx + 56 + sway, cy + 14, 5, furDark);
  c.drawWideLine(cx + 56 + sway, cy + 14, cx + 52 + sway * 1.6f, cy - 12, 5, furDark);
  // Ears: twitch while listening, flat when sad.
  int twitch = pose == Pose::Listening ? (int)(wave(t, 300) * 4) : 0;
  for (int s = -1; s <= 1; s += 2) {
    int tipX = pose == Pose::Sad ? cx + s * 54 : cx + s * (32 + twitch);
    int tipY = pose == Pose::Sad ? cy - 26 : cy - 50;
    c.fillTriangle(cx + s * 40, cy - 12, tipX, tipY, cx + s * 12, cy - 28, fur);
    c.fillTriangle(cx + s * 34, cy - 16, (tipX + cx + s * 34) / 2, (tipY + cy - 16) / 2 + 2, cx + s * 18, cy - 26, pink);
  }
  c.fillEllipse(cx, cy + 4, 42, 36, fur);
  // Eyes with slit pupils.
  int dy = pose == Pose::Thinking ? -3 : 0;
  for (int s = -1; s <= 1; s += 2) {
    int ex = cx + s * 15;
    if (blinking(t) && pose != Pose::Thinking) {
      c.fillRect(ex - 8, cy - 4, 16, 2, kBlack);
    } else {
      c.fillEllipse(ex, cy - 4, 8, pose == Pose::Sad ? 5 : 9, eye);
      c.fillEllipse(ex + (pose == Pose::Thinking ? 2 : 0), cy - 4 + dy, 2, pose == Pose::Sad ? 4 : 7, kBlack);
    }
  }
  c.fillTriangle(cx - 5, cy + 8, cx + 5, cy + 8, cx, cy + 13, pink);  // nose
  c.fillArc(cx - 4, cy + 14, 3, 5, 0, 180, kBlack);                   // "w" mouth
  c.fillArc(cx + 4, cy + 14, 3, 5, 0, 180, kBlack);
  if (pose == Pose::Speaking) {
    int o = openness(level, 12, t);
    if (o) c.fillEllipse(cx, cy + 22, 6, o / 2 + 1, kMouth);
  }
  int wt = pose == Pose::Speaking && level > 10 ? (int)((t / 80) % 2) * 2 : 0;
  for (int i = 0; i < 3; ++i) {
    c.drawLine(cx - 20, cy + 10 + i * 4, cx - 50, cy + 4 + i * 8 + wt, whisker);
    c.drawLine(cx + 20, cy + 10 + i * 4, cx + 50, cy + 4 + i * 8 + wt, whisker);
  }
}

// ── dog ─────────────────────────────────────────────────────────────

void drawDog(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t fur = rgb(205, 160, 110), ear = rgb(120, 80, 50), muzzle = rgb(240, 215, 180);
  const uint16_t pink = rgb(240, 110, 130);
  int tilt = pose == Pose::Listening ? 6 : 0;          // the classic head tilt
  int sad = pose == Pose::Sad ? 8 : 0;
  c.fillEllipse(cx - 40, cy + 4 - tilt + sad, 12, 28, ear);
  c.fillEllipse(cx + 40, cy + 4 + tilt + sad, 12, 28, ear);
  c.fillEllipse(cx, cy - 2, 38, 38, fur);
  c.fillEllipse(cx, cy + 18, 22, 16, muzzle);
  // Eyes (bigger puppy eyes when sad).
  int er = pose == Pose::Sad ? 7 : 5;
  for (int s = -1; s <= 1; s += 2) {
    int ex = cx + s * 14, ey = cy - 10 + (s < 0 ? -tilt / 2 : tilt / 2);
    if (blinking(t) && pose == Pose::Idle) {
      c.fillRect(ex - 5, ey, 10, 2, kBlack);
    } else {
      c.fillCircle(ex, ey, er, kBlack);
      c.fillCircle(ex - 2, ey - 2, er / 3 + 1, kWhite);
    }
    if (pose == Pose::Sad) c.drawLine(ex - 8 * s, ey - 12, ex + 4 * s, ey - 9, ear);
  }
  int sniff = pose == Pose::Thinking ? (int)(wave(t, 240) * 2) : 0;
  c.fillEllipse(cx, cy + 8 + sniff, 9, 6, kBlack);
  c.drawFastVLine(cx, cy + 14, 8, kBlack);
  if (pose == Pose::Speaking) {
    int o = openness(level, 14, t);
    c.fillEllipse(cx, cy + 26, 12, o / 2 + 2, kMouth);
    if (o > 6) c.fillEllipse(cx, cy + 27 + o / 4, 7, o / 4 + 1, pink);
  } else {
    c.drawLine(cx, cy + 22, cx - 8, cy + 25, kBlack);
    c.drawLine(cx, cy + 22, cx + 8, cy + 25, kBlack);
    if (pose != Pose::Sad) {  // tongue out, panting
      int pant = (int)(wave(t, 500) * 2);
      c.fillEllipse(cx + 4, cy + 32 + pant, 6, 8, pink);
      c.drawFastVLine(cx + 4, cy + 28 + pant, 8, rgb(200, 80, 100));
    }
  }
}

// ── dinosaur (T-rex, facing right) ──────────────────────────────────

void drawDinosaur(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t green = rgb(80, 170, 80), dark = rgb(50, 120, 50), belly = rgb(200, 225, 150);
  bool down = pose == Pose::Sad;                          // lying down, eyes shut
  int yo = down ? 14 : 0;
  int hy = pose == Pose::Listening ? -6 : (down ? 10 : 0);  // head lifts to listen
  c.fillTriangle(cx - 84, cy + 20 + yo, cx - 30, cy - 4 + yo, cx - 28, cy + 26 + yo, green);  // tail
  if (!down) {
    c.fillRoundRect(cx - 32, cy + 28, 12, 24, 4, dark);   // legs
    c.fillRoundRect(cx - 8, cy + 28, 12, 24, 4, dark);
  }
  c.fillEllipse(cx - 14, cy + 12 + yo, 34, 26, green);    // body
  c.fillEllipse(cx - 8, cy + 20 + yo, 22, 14, belly);
  for (int i = 0; i < 4; ++i) {                          // back spikes
    int x = cx - 40 + i * 12;
    c.fillTriangle(x, cy - 8 + yo + i, x + 6, cy - 18 + yo + i, x + 12, cy - 8 + yo + i, dark);
  }
  c.fillRoundRect(cx - 2, cy - 20 + hy + yo, 24, 36, 10, green);  // neck
  int hx = cx + 6, hTop = cy - 36 + hy + yo;
  int o = pose == Pose::Speaking ? openness(level, 16, t) : (down ? 0 : 2);
  c.fillRoundRect(hx + 4, hTop + 22 + o, 46, 12, 5, green);       // lower jaw
  if (o > 0) c.fillRect(hx + 8, hTop + 22, 40, o, kMouth);
  c.fillRoundRect(hx, hTop, 56, 26, 10, green);                   // upper head
  for (int i = 0; i < 5; ++i) {                                   // teeth
    int x = hx + 14 + i * 8;
    c.fillTriangle(x, hTop + 25, x + 5, hTop + 25, x + 2, hTop + 29, kWhite);
  }
  c.fillCircle(hx + 50, hTop + 8, 2, dark);                       // nostril
  if (down || (blinking(t) && pose == Pose::Idle)) {
    c.fillRect(hx + 16, hTop + 9, 10, 2, kBlack);
  } else {
    c.fillCircle(hx + 21, hTop + 9, 5, kWhite);
    c.fillCircle(hx + 22 + (pose == Pose::Thinking ? 1 : 0), hTop + 9 - (pose == Pose::Thinking ? 2 : 0), 2, kBlack);
  }
  // Tiny arms — one scratches its head while thinking.
  if (pose == Pose::Thinking) {
    float s = wave(t, 300) * 3;
    c.drawWideLine(cx + 8, cy + 6, cx + 14 + s, hTop + 28, 2.5f, dark);
  } else {
    c.drawWideLine(cx + 8, cy + 8 + yo, cx + 20, cy + 14 + yo, 2.5f, dark);
  }
}

}  // namespace face
