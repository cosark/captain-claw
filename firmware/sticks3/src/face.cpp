// Character dispatch, shared helpers, and the original three (captain, bot, bird).
#include "face_common.h"

namespace face {

static constexpr uint16_t kWave = rgb(90, 200, 255);
static constexpr uint16_t kDots = rgb(200, 200, 200);

static String spoken;
static Pose lastPose = Pose::Idle;
static uint8_t lastKind = 255;
static uint32_t poseStart = 0;

const char* name(uint8_t kind) {
  static const char* names[KindCount] = {
      "Captain", "Bot", "Bird", "Car", "Matrix", "Orc", "Elf", "Dwarf", "C64", "Amiga",
      "Cat", "Dog", "Dinosaur", "Alien", "Caveman", "HAL 9000", "KITT", "Pac-Man"};
  return kind < KindCount ? names[kind] : names[0];
}

void setText(const String& text) { spoken = text; }
const String& sayText() { return spoken; }
uint32_t poseAge(uint32_t t) { return t - poseStart; }

uint32_t frameMs(uint8_t kind) {
  switch (kind) {
    case Matrix: case Amiga: case Kitt: case PacMan: case Hal: return 50;
    default: return 120;
  }
}

bool blinking(uint32_t t) { return t % 3600 < 130; }

int openness(int level, int maxOpen, uint32_t t) {
  if (level < 6) return 0;
  int o = level * maxOpen / 100;
  return constrain(o + (int)((t / 70) % 3) - 1, 1, maxOpen);
}

float wave(uint32_t t, uint32_t periodMs) {
  return sinf((t % periodMs) * (6.2831853f / periodMs));
}

// "Sound coming in" arcs left of the character, one per ~30% of mic level.
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

// Screens and machines that show listening/thinking their own way.
static bool ownsPoses(uint8_t kind) {
  switch (kind) {
    case Matrix: case C64: case Amiga: case Hal: case Kitt: case PacMan: case Dinosaur: return true;
    default: return false;
  }
}

void draw(M5Canvas& c, uint8_t kind, Pose pose, int level, uint32_t t, int cx, int cy) {
  if (pose != lastPose || kind != lastKind) {
    lastPose = pose;
    lastKind = kind;
    poseStart = t;
  }
  switch (kind) {
    case Bot: drawBot(c, pose, level, t, cx, cy); break;
    case Bird: drawBird(c, pose, level, t, cx, cy); break;
    case Car: drawCar(c, pose, level, t, cx, cy); break;
    case Matrix: drawMatrix(c, pose, level, t, cx, cy); break;
    case Orc: drawOrc(c, pose, level, t, cx, cy); break;
    case Elf: drawElf(c, pose, level, t, cx, cy); break;
    case Dwarf: drawDwarf(c, pose, level, t, cx, cy); break;
    case C64: drawC64(c, pose, level, t, cx, cy); break;
    case Amiga: drawAmiga(c, pose, level, t, cx, cy); break;
    case Cat: drawCat(c, pose, level, t, cx, cy); break;
    case Dog: drawDog(c, pose, level, t, cx, cy); break;
    case Dinosaur: drawDinosaur(c, pose, level, t, cx, cy); break;
    case Alien: drawAlien(c, pose, level, t, cx, cy); break;
    case Caveman: drawCaveman(c, pose, level, t, cx, cy); break;
    case Hal: drawHal(c, pose, level, t, cx, cy); break;
    case Kitt: drawKitt(c, pose, level, t, cx, cy); break;
    case PacMan: drawPacMan(c, pose, level, t, cx, cy); break;
    default: drawCaptain(c, pose, level, t, cx, cy);
  }
  if (!ownsPoses(kind)) {
    if (pose == Pose::Listening) listenWaves(c, cx - 70, cy, level);
    if (pose == Pose::Thinking) thinkDots(c, cx + 52, cy - 30, t);
  }
}

// ── bot ─────────────────────────────────────────────────────────────

void drawBot(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
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

void drawCaptain(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
  const uint16_t skin = rgb(245, 196, 160), beard = rgb(235, 235, 235), navy = rgb(25, 40, 95);
  const uint16_t gold = rgb(240, 190, 40);
  int fy = cy + 8;
  c.fillCircle(cx, fy, 37, skin);
  c.fillEllipse(cx, fy + 22, 34, 20, beard);           // beard
  c.fillRoundRect(cx - 40, cy - 48, 80, 28, 9, navy);  // cap crown
  c.fillRect(cx - 40, cy - 25, 80, 6, kWhite);         // band
  c.fillEllipse(cx, cy - 18, 43, 6, kBlack);           // visor
  c.fillCircle(cx, cy - 36, 7, gold);                  // badge
  c.fillCircle(cx, cy - 36, 3, navy);
  int dx = pose == Pose::Thinking ? 3 : 0, dy = pose == Pose::Thinking ? -2 : 0;
  if (blinking(t) && pose != Pose::Thinking) {
    c.fillRect(cx - 18, fy - 6, 9, 2, kBlack);
    c.fillRect(cx + 9, fy - 6, 9, 2, kBlack);
  } else {
    c.fillCircle(cx - 13 + dx, fy - 6 + dy, 4, kBlack);
    c.fillCircle(cx + 13 + dx, fy - 6 + dy, 4, kBlack);
  }
  c.fillCircle(cx, fy + 4, 5, rgb(230, 140, 120));     // nose
  c.fillEllipse(cx - 10, fy + 12, 11, 4, beard);       // moustache
  c.fillEllipse(cx + 10, fy + 12, 11, 4, beard);
  int o = pose == Pose::Speaking ? openness(level, 14, t) : 0;
  if (pose == Pose::Sad) {
    c.fillArc(cx, fy + 30, 9, 11, 210, 330, kMouth);
  } else {
    c.fillEllipse(cx, fy + 20, 9, o / 2 + 2, kMouth);
  }
}

// ── bird ────────────────────────────────────────────────────────────

void drawBird(M5Canvas& c, Pose pose, int level, uint32_t t, int cx, int cy) {
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

}  // namespace face
