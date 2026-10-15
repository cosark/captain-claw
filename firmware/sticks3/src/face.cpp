#include "face.h"

namespace face {

static constexpr uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

static constexpr uint16_t kBlack = rgb(0, 0, 0);
static constexpr uint16_t kWhite = rgb(255, 255, 255);
static constexpr uint16_t kMouth = rgb(110, 20, 30);
static constexpr uint16_t kTongue = rgb(230, 90, 100);
static constexpr uint16_t kWave = rgb(90, 200, 255);
static constexpr uint16_t kDots = rgb(200, 200, 200);

const char* name(uint8_t kind) {
  switch (kind) {
    case Bot: return "bot";
    case Captain: return "captain";
    case Bird: return "bird";
    default: return "smiley";
  }
}

static bool blinking(uint32_t t) { return t % 3600 < 130; }

// Mouth opening in px for a speaking level, with a little jitter so it moves
// like speech rather than a meter.
static int openness(int level, int maxOpen, uint32_t t) {
  if (level < 6) return 0;
  int o = level * maxOpen / 100;
  return constrain(o + (int)((t / 70) % 3) - 1, 1, maxOpen);
}

// "Sound coming in" arcs left of the character, one per 25% of mic level.
static void listenWaves(M5Canvas& c, int x, int y, int level) {
  for (int i = 0; i < 3; ++i) {
    uint16_t col = level > 8 + i * 28 ? kWave : rgb(40, 60, 80);
    int r = 10 + i * 9;
    c.fillArc(x, y, r, r + 3, 135, 225, col);
  }
}

// Three thought dots appearing one by one above-right.
static void thinkDots(M5Canvas& c, int x, int y, uint32_t t) {
  int n = (t / 350) % 4;
  for (int i = 0; i < n; ++i) c.fillCircle(x + i * 11, y - i * 5, 3 + i, kDots);
}

// ── smiley ──────────────────────────────────────────────────────────

static void smiley(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const int R = 44;
  c.fillCircle(cx, cy, R, rgb(255, 205, 40));
  int ex = 16, ey = cy - 12, dx = 0, dy = 0;
  if (pose == Pose::Thinking) dx = 3, dy = -4;
  if (blinking(t) && pose != Pose::Thinking) {
    c.fillRect(cx - ex - 5, ey, 10, 3, kBlack);
    c.fillRect(cx + ex - 5, ey, 10, 3, kBlack);
  } else {
    c.fillEllipse(cx - ex + dx, ey + dy, 5, 8, kBlack);
    c.fillEllipse(cx + ex + dx, ey + dy, 5, 8, kBlack);
  }
  switch (pose) {
    case Pose::Speaking: {
      int o = openness(level, 20, t);
      if (o == 0) {
        c.fillArc(cx, cy + 4, 20, 23, 30, 150, kBlack);
      } else {
        c.fillEllipse(cx, cy + 20, 15, o / 2 + 3, kMouth);
        if (o > 8) c.fillEllipse(cx, cy + 22 + o / 4, 8, o / 5 + 1, kTongue);
      }
      break;
    }
    case Pose::Thinking:
      c.fillCircle(cx + 8, cy + 22, 5, kMouth);
      break;
    case Pose::Sad:
      c.fillArc(cx, cy + 36, 18, 21, 210, 330, kBlack);
      break;
    default:
      c.fillArc(cx, cy + 2, 26, 30, 30, 150, kBlack);
  }
}

// ── bot ─────────────────────────────────────────────────────────────

static void bot(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t body = rgb(150, 160, 175), plate = rgb(30, 36, 48), led = rgb(60, 230, 255);
  c.drawFastVLine(cx, cy - 46, 12, body);
  bool lamp = pose == Pose::Thinking ? (t / 300) % 2 : true;
  c.fillCircle(cx, cy - 48, 5, lamp ? rgb(255, 70, 70) : rgb(90, 20, 20));
  c.fillRoundRect(cx - 52, cy - 12, 8, 24, 3, body);
  c.fillRoundRect(cx + 44, cy - 12, 8, 24, 3, body);
  c.fillRoundRect(cx - 44, cy - 36, 88, 76, 14, body);
  c.fillRoundRect(cx - 36, cy - 28, 72, 58, 9, plate);
  int dx = pose == Pose::Thinking ? 4 : 0, dy = pose == Pose::Thinking ? -3 : 0;
  int eh = blinking(t) && pose != Pose::Thinking ? 2 : 11;
  c.fillRoundRect(cx - 26 + dx, cy - 14 + dy + (11 - eh) / 2, 16, eh, 3, led);
  c.fillRoundRect(cx + 10 + dx, cy - 14 + dy + (11 - eh) / 2, 16, eh, 3, led);
  // Mouth: a grille of bars that dance while speaking.
  for (int i = 0; i < 7; ++i) {
    int x = cx - 24 + i * 7;
    int h = 2;
    if (pose == Pose::Speaking && level > 5) {
      int wobble = (int)((t / 60 + i * 37) % 7);
      h = constrain(level * 14 / 100 + wobble - 3, 2, 16);
    } else if (pose == Pose::Sad) {
      h = 2 + abs(3 - i);
    }
    c.fillRect(x, cy + 16 - h / 2, 5, h, led);
  }
}

// ── captain ─────────────────────────────────────────────────────────

static void captain(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t skin = rgb(245, 196, 160), beard = rgb(235, 235, 235), navy = rgb(25, 40, 95);
  const uint16_t gold = rgb(240, 190, 40);
  int fy = cy + 8;
  c.fillCircle(cx, fy, 37, skin);
  c.fillEllipse(cx, fy + 22, 34, 20, beard);        // beard
  c.fillRoundRect(cx - 40, cy - 48, 80, 28, 9, navy);  // cap crown
  c.fillRect(cx - 40, cy - 25, 80, 6, kWhite);      // band
  c.fillEllipse(cx, cy - 18, 43, 6, kBlack);         // visor
  c.fillCircle(cx, cy - 36, 7, gold);                // badge
  c.fillCircle(cx, cy - 36, 3, navy);
  int dx = pose == Pose::Thinking ? 3 : 0, dy = pose == Pose::Thinking ? -2 : 0;
  if (blinking(t) && pose != Pose::Thinking) {
    c.fillRect(cx - 18, fy - 6, 9, 2, kBlack);
    c.fillRect(cx + 9, fy - 6, 9, 2, kBlack);
  } else {
    c.fillCircle(cx - 13 + dx, fy - 6 + dy, 4, kBlack);
    c.fillCircle(cx + 13 + dx, fy - 6 + dy, 4, kBlack);
  }
  c.fillCircle(cx, fy + 4, 5, rgb(230, 140, 120));   // nose
  c.fillEllipse(cx - 10, fy + 12, 11, 4, beard);     // moustache
  c.fillEllipse(cx + 10, fy + 12, 11, 4, beard);
  int o = pose == Pose::Speaking ? openness(level, 14, t) : 0;
  if (pose == Pose::Sad) {
    c.fillArc(cx, fy + 30, 9, 11, 210, 330, kMouth);
  } else {
    c.fillEllipse(cx, fy + 20, 9, o / 2 + 2, kMouth);
  }
}

// ── bird ────────────────────────────────────────────────────────────

static void bird(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t feather = rgb(70, 160, 240), dark = rgb(40, 100, 190);
  const uint16_t beakTop = rgb(255, 160, 30), beakLow = rgb(230, 110, 20);
  int bx = cx - 6;
  c.fillTriangle(bx - 6, cy - 38, bx + 4, cy - 52, bx + 8, cy - 36, dark);   // crest
  c.fillTriangle(bx + 2, cy - 38, bx + 16, cy - 50, bx + 16, cy - 34, dark);
  c.fillCircle(bx, cy + 2, 40, feather);
  c.fillEllipse(bx + 2, cy + 18, 24, 20, rgb(240, 245, 250));           // belly
  c.fillEllipse(bx - 26, cy + 10, 13, 22, dark);                         // wing
  int ex = bx + 14, ey = cy - 12;
  if (blinking(t) && pose != Pose::Thinking) {
    c.fillRect(ex - 7, ey, 14, 3, kBlack);
  } else {
    c.fillCircle(ex, ey, 9, kWhite);
    int dx = pose == Pose::Thinking ? 2 : 3, dy = pose == Pose::Thinking ? -4 : 0;
    c.fillCircle(ex + dx, ey + dy, 4, kBlack);
  }
  int o = pose == Pose::Speaking ? openness(level, 12, t) : (pose == Pose::Sad ? 0 : 1);
  int beakX = bx + 34;
  c.fillTriangle(beakX, cy - 6, beakX + 24, cy, beakX, cy + 4, beakTop);
  c.fillTriangle(beakX, cy + 4, beakX + 20, cy + 4 + o, beakX, cy + 8 + o / 2, beakLow);
  c.fillRect(bx - 12, cy + 41, 3, 8, beakLow);                          // feet
  c.fillRect(bx + 8, cy + 41, 3, 8, beakLow);
}

void draw(M5Canvas& c, uint8_t kind, Pose pose, int level, uint32_t t, int cx, int cy) {
  switch (kind) {
    case Bot: bot(c, pose, level, t, cx, cy); break;
    case Captain: captain(c, pose, level, t, cx, cy); break;
    case Bird: bird(c, pose, level, t, cx, cy); break;
    default: smiley(c, pose, level, t, cx, cy);
  }
  if (pose == Pose::Listening) listenWaves(c, cx - 58, cy, level);
  if (pose == Pose::Thinking) thinkDots(c, cx + 50, cy - 30, t);
}

}  // namespace face
