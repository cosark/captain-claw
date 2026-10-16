// Screen: 240×135 landscape, drawn into an off-screen canvas then pushed
// (no flicker). Text uses efontJA, which has č ć š ž đ.
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

// Health of a link, shown as a colour block in the top bar.
enum Health : uint8_t { Down = 0, Partial = 1, Up = 2 };

void begin();
void brightness(uint8_t level);

// Full-screen notices.
void showSetup(const char* reason, const char* apName, const char* ip);
void showMessage(const char* title, const String& body, uint16_t color = 0xFFFF);

// Top bar: Wi-Fi + agent blocks, agent name, state, battery % (redrawn with every view).
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

// Conversation views (text mode).
void showListening(int level, uint32_t elapsedMs);
// While the agent works: what it heard, its latest activity lines (oldest
// first), and the last narration / phase line at the bottom.
void showWorking(const String& heard, const std::vector<String>& activity, const String& status);
void setReply(const String& text);  // wraps into pages (shown by showReply)
// Reading text size: 0 small, 1 medium, 2 large (re-wraps the current reply).
void setTextSize(uint8_t size);
void showReply();                   // redraw the current page
bool nextPage();                    // false when it wrapped back to page 1
bool hasReply();

// Icon mode: the character in a pose, under the top bar.
void showFace(uint8_t kind, face::Pose pose, int level);

// Pictures (full screen, with a small health/battery overlay).
void storeImage(const uint8_t* jpg, size_t len);
bool hasImage();
void showStoredImage();

void showPicker(const std::vector<String>& names, int selected, int current);

// Attention flash before speech: on = the screen is white.
void flashFrame(bool on);

}  // namespace ui
