// The screen. Each device has its own implementation of this interface:
//   ui_stick.cpp  StickS3: 240×135 landscape LCD, efont, trimmed efontJA (has č ć š ž đ)
//   ui_round.cpp  StopWatch: 466×466 round AMOLED, smooth Inter fonts
// Both draw into an off-screen canvas and push it whole (no flicker).
//
// Colours are RGB565 (uint16_t) throughout — M5GFX reads a uint32_t colour
// as RGB888, so passing TFT_* constants through uint32_t draws wrong colours.
#pragma once

#include <Arduino.h>

#include <vector>

#include "face.h"

namespace ui {

constexpr uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

// Health of a link, shown as a colour block (stick) or dot (round).
enum Health : uint8_t { Down = 0, Partial = 1, Up = 2 };

void begin();
void brightness(uint8_t level);

// True when screens animate on their own (the round screen's ring and
// timers): the main loop then redraws them at frame rate.
bool animated();

// Full-screen notices.
void showSetup(const char* reason, const char* apName, const char* ip);
void showMessage(const char* title, const String& body, uint16_t color = 0xFFFF);

// Status bar: Wi-Fi + agent health, agent name, state, battery (redrawn with every view).
void setBar(const String& agent, const char* state, uint16_t stateColor);
void setHealth(Health wifi, Health agent);

// One labelled line on the status page / idle panel.
struct Row {
  String label;
  String value;
  uint16_t color;
};
// Draws page `page` of the rows (in the chosen text size); returns the page
// count. The title shows on the first page only.
int showPanel(const char* title, uint16_t titleColor, const std::vector<Row>& rows, int page = 0);

// The main screen when nothing is going on (text mode). The stick shows the
// title and rows as a panel; the round screen shows the agent's badge with
// the hint under it.
struct Home {
  bool ready;
  const char* title;       // "All good" / "Getting ready"
  uint16_t color;          // the title's / hint's colour
  String hint;             // "hold the yellow button to talk", "waiting for agent"…
  std::vector<Row> rows;   // device, Wi-Fi, deck, agent
};
void showHome(const Home& home);

// Conversation views (text mode).
void showListening(int level, uint32_t elapsedMs);
// While the agent works: what it heard, its latest activity lines (oldest
// first), the last narration / phase line, and the time and tool steps so far.
void showWorking(const String& heard, const std::vector<String>& activity, const String& status,
                 uint32_t elapsedMs, int steps);
void setReply(const String& text);  // wraps into pages (shown by showReply)
// Reading text size: 0 small, 1 medium, 2 large (re-wraps the current reply).
void setTextSize(uint8_t size);
void showReply();                   // redraw the current page
bool nextPage();                    // false when it wrapped back to page 1
bool hasReply();

// Icon mode: the character in a pose.
void showFace(uint8_t kind, face::Pose pose, int level);
// Menu → Character: the character being previewed, `index` of KindCount.
void showCharPick(uint8_t kind, face::Pose pose, int level, int index);

// Pictures (full screen, with a small health/battery overlay on the stick).
// Takes the bytes over (they were ps_malloc'ed) and frees the last picture.
void storeImage(uint8_t* jpg, size_t len);
bool hasImage();
void showStoredImage();

// One entry of a menu or list. The stick shows `label` as one line; the
// round screen shows a card: `badge` big in a glowing badge (a value like
// "75%"), or a glyph — Initials turns `badge` (a name) into its initials —
// with `title` under it, then `note`.
enum class Glyph : uint8_t { None, Initials, Wifi, Back, Info };
struct Item {
  String label;
  String title;
  String badge;
  String note;
  Glyph glyph = Glyph::None;
  bool on = true;  // false: a switched-off setting (a dim badge)
};
void showPicker(const std::vector<Item>& items, int selected, int current);

// Attention flash before speech: on = the screen (stick) / ring (round) is white.
void flashFrame(bool on);

}  // namespace ui
