// Screen: 240×135 landscape, drawn into an off-screen canvas then pushed
// (no flicker). Text uses efontJA, which has č ć š ž đ.
#pragma once

#include <Arduino.h>

#include <vector>

namespace ui {

void begin();
void brightness(uint8_t level);  // 0 = backlight off

// Full-screen notices.
void showSetup(const char* reason, const char* apName, const char* ip);
void showMessage(const char* title, const String& body, uint32_t color = 0xFFFFFF);

// Top bar: agent name + state word (redrawn with every view).
void setBar(const String& agent, const char* state, uint32_t stateColor);

// Conversation views.
void showListening(int level, uint32_t elapsedMs);
// While the agent works: what it heard, its latest activity lines (oldest
// first), and the last narration / phase line at the bottom.
void showWorking(const String& heard, const std::vector<String>& activity, const String& status);
void setReply(const String& text);  // wraps into pages, shows page 1
void showReply();                   // redraw the current page
bool nextPage();                    // false when it wrapped back to page 1
bool hasReply();
void showImage(const uint8_t* jpg, size_t len);
bool hasImage();
void showStoredImage();
void showPicker(const std::vector<String>& names, int selected, int current);

}  // namespace ui
