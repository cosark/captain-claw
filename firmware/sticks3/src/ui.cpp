#include "ui.h"

#include <M5Unified.h>

namespace ui {

static M5Canvas canvas(&M5.Display);
static constexpr int W = 240, H = 135, BAR = 16, PAD = 4;
static const lgfx::U8g2font* kBody = &fonts::efontJA_14;
static const lgfx::U8g2font* kSmall = &fonts::efontJA_12;
static constexpr int kLine = 16;

static String barAgent = "";
static String barState = "";
static uint32_t barColor = TFT_DARKGREY;

static std::vector<String> lines;
static int page = 0;
static uint8_t* imgBuf = nullptr;
static size_t imgLen = 0;

static int linesPerPage() { return (H - BAR - PAD) / kLine; }

void begin() {
  M5.Display.setRotation(1);
  canvas.setPsram(true);
  canvas.setColorDepth(16);
  canvas.createSprite(W, H);
  canvas.setTextWrap(false);
}

void brightness(uint8_t level) { M5.Display.setBrightness(level); }

static void push() { canvas.pushSprite(0, 0); }

static void drawBar() {
  canvas.fillRect(0, 0, W, BAR, 0x18E3);  // dark slate
  canvas.setFont(kSmall);
  canvas.setTextDatum(middle_left);
  canvas.setTextColor(TFT_WHITE);
  // Battery on the far right, state left of it, agent name in what remains.
  int bx = W - 26, by = 4;
  int nameRoom = bx - 6 - canvas.textWidth(barState) - 8 - PAD;
  String name = barAgent.length() ? barAgent : String("Captain Claw");
  while (name.length() > 1 && canvas.textWidth(name) > nameRoom) name.remove(name.length() - 1);
  canvas.drawString(name, PAD, BAR / 2);

  int lvl = M5.Power.getBatteryLevel();
  bool chg = M5.Power.isCharging() == m5::Power_Class::is_charging;
  canvas.drawRect(bx, by, 20, 9, TFT_LIGHTGREY);
  canvas.fillRect(bx + 20, by + 3, 2, 3, TFT_LIGHTGREY);
  int fill = lvl < 0 ? 0 : max(1, lvl * 18 / 100);
  canvas.fillRect(bx + 1, by + 1, fill, 7, chg ? TFT_GREEN : (lvl >= 0 && lvl < 20 ? TFT_RED : TFT_WHITE));

  canvas.setTextDatum(middle_right);
  canvas.setTextColor(barColor);
  canvas.drawString(barState, bx - 6, BAR / 2);
}

void setBar(const String& agent, const char* state, uint32_t stateColor) {
  barAgent = agent;
  barState = state ? state : "";
  barColor = stateColor;
}

// Word-wrap UTF-8 text to the body width (long words break between characters).
static std::vector<String> wrap(const String& text, int width) {
  std::vector<String> out;
  canvas.setFont(kBody);
  int start = 0;
  const int n = text.length();
  while (start <= n) {
    int nl = text.indexOf('\n', start);
    String para = text.substring(start, nl < 0 ? n : nl);
    start = (nl < 0 ? n : nl) + 1;
    String line;
    int i = 0;
    while (i < (int)para.length()) {
      int sp = para.indexOf(' ', i);
      String word = para.substring(i, sp < 0 ? para.length() : sp);
      i = sp < 0 ? para.length() : sp + 1;
      String trial = line.length() ? line + " " + word : word;
      if (canvas.textWidth(trial) <= width) {
        line = trial;
        continue;
      }
      if (line.length()) out.push_back(line);
      line = "";
      // A word wider than the screen: split it between UTF-8 characters.
      while (canvas.textWidth(word) > width) {
        int cut = 0, k = 0;
        while (k < (int)word.length()) {
          int step = 1;
          uint8_t c = word[k];
          if (c >= 0xF0) step = 4; else if (c >= 0xE0) step = 3; else if (c >= 0xC0) step = 2;
          if (canvas.textWidth(word.substring(0, k + step)) > width) break;
          k += step;
          cut = k;
        }
        if (cut == 0) cut = 1;
        out.push_back(word.substring(0, cut));
        word = word.substring(cut);
      }
      line = word;
    }
    out.push_back(line);
    if (nl < 0) break;
  }
  while (!out.empty() && out.back().isEmpty()) out.pop_back();
  return out;
}

void showSetup(const char* reason, const char* apName, const char* ip) {
  canvas.fillScreen(TFT_BLACK);
  String qr = String("WIFI:T:nopass;S:") + apName + ";;";
  canvas.qrcode(qr.c_str(), 2, 2, 110, 3);
  canvas.setFont(kSmall);
  canvas.setTextDatum(top_left);
  canvas.setTextColor(TFT_ORANGE);
  canvas.drawString(reason, 118, 4);
  canvas.setTextColor(TFT_WHITE);
  canvas.drawString("1. Scan or join", 118, 26);
  canvas.setTextColor(TFT_CYAN);
  canvas.drawString(apName, 118, 42);
  canvas.setTextColor(TFT_WHITE);
  canvas.drawString("2. Open", 118, 66);
  canvas.setTextColor(TFT_CYAN);
  canvas.drawString(String("http://") + ip, 118, 82);
  canvas.setTextColor(TFT_DARKGREY);
  canvas.drawString("Save restarts", 118, 110);
  push();
}

void showMessage(const char* title, const String& body, uint32_t color) {
  canvas.fillScreen(TFT_BLACK);
  drawBar();
  canvas.setFont(kBody);
  canvas.setTextDatum(top_left);
  canvas.setTextColor(color);
  canvas.drawString(title, PAD, BAR + 6);
  canvas.setTextColor(TFT_LIGHTGREY);
  auto ls = wrap(body, W - 2 * PAD);
  for (int i = 0; i < (int)ls.size() && i < linesPerPage() - 1; ++i) {
    canvas.drawString(ls[i], PAD, BAR + 6 + kLine * (i + 1) + 4);
  }
  push();
}

void showListening(int level, uint32_t elapsedMs) {
  canvas.fillScreen(TFT_BLACK);
  drawBar();
  canvas.fillCircle(30, 70, 14, TFT_RED);
  canvas.setFont(&fonts::FreeSansBold12pt7b);
  canvas.setTextDatum(middle_left);
  canvas.setTextColor(TFT_WHITE);
  canvas.drawString("Listening", 56, 64);
  canvas.setFont(kSmall);
  canvas.setTextColor(TFT_DARKGREY);
  char t[16];
  snprintf(t, sizeof(t), "%lu.%lus", (unsigned long)(elapsedMs / 1000), (unsigned long)(elapsedMs / 100 % 10));
  canvas.drawString(t, 56, 88);
  canvas.drawString("release to send", 120, 88);
  int bar = (W - 2 * PAD) * min(level, 100) / 100;
  canvas.fillRect(PAD, H - 12, W - 2 * PAD, 6, 0x2104);
  canvas.fillRect(PAD, H - 12, bar, 6, level > 85 ? TFT_ORANGE : TFT_GREEN);
  push();
}

static String fit(const String& text, int width) {
  if (canvas.textWidth(text) <= width) return text;
  String s = text;
  while (s.length() > 1 && canvas.textWidth(s + "…") > width) {
    int cut = s.length() - 1;
    while (cut > 0 && (uint8_t(s[cut]) & 0xC0) == 0x80) --cut;  // whole UTF-8 characters
    s.remove(cut);
  }
  return s + "…";
}

void showWorking(const String& heard, const std::vector<String>& activity, const String& status) {
  canvas.fillScreen(TFT_BLACK);
  drawBar();
  canvas.setTextDatum(top_left);
  int y = BAR + 3;
  if (heard.length()) {  // the end of a long sentence matters most: last two lines
    canvas.setFont(kBody);
    canvas.setTextColor(TFT_CYAN);
    auto ls = wrap(String("“") + heard + "”", W - 2 * PAD);
    int maxLines = activity.empty() ? 5 : 2;
    for (int i = max(0, (int)ls.size() - maxLines); i < (int)ls.size(); ++i, y += kLine) {
      canvas.drawString(ls[i], PAD, y);
    }
    y += 3;
  }
  canvas.setFont(kSmall);
  constexpr int kSmallLine = 15;
  int room = (H - 16 - y) / kSmallLine;  // keep the bottom line free
  int first = max(0, (int)activity.size() - room);
  for (int i = first; i < (int)activity.size(); ++i, y += kSmallLine) {
    bool newest = i == (int)activity.size() - 1;
    canvas.setTextColor(newest ? TFT_WHITE : 0x8410);  // older lines grey
    canvas.drawString(fit((newest ? "→ " : "   ") + activity[i], W - 2 * PAD), PAD, y);
  }
  if (status.length()) {
    canvas.setTextColor(TFT_DARKGREY);
    canvas.drawString(fit(status, W - 2 * PAD), PAD, H - 14);
  }
  push();
}

void setReply(const String& text) {
  lines = wrap(text, W - 2 * PAD - 4);
  page = 0;
  showReply();
}

bool hasReply() { return !lines.empty(); }

void showReply() {
  canvas.fillScreen(TFT_BLACK);
  drawBar();
  canvas.setFont(kBody);
  canvas.setTextDatum(top_left);
  canvas.setTextColor(TFT_WHITE);
  int per = linesPerPage();
  int pages = max(1, ((int)lines.size() + per - 1) / per);
  int first = page * per;
  for (int i = 0; i < per && first + i < (int)lines.size(); ++i) {
    canvas.drawString(lines[first + i], PAD, BAR + PAD + i * kLine);
  }
  if (pages > 1) {  // scroll position on the right edge
    int track = H - BAR - 4;
    int thumb = max(6, track / pages);
    int ty = BAR + 2 + (track - thumb) * page / (pages - 1);
    canvas.fillRect(W - 3, BAR + 2, 2, track, 0x2104);
    canvas.fillRect(W - 3, ty, 2, thumb, TFT_LIGHTGREY);
  }
  push();
}

bool nextPage() {
  int per = linesPerPage();
  int pages = max(1, ((int)lines.size() + per - 1) / per);
  page = (page + 1) % pages;
  showReply();
  return page != 0;
}

void showImage(const uint8_t* jpg, size_t len) {
  if (imgBuf) free(imgBuf);
  imgBuf = (uint8_t*)ps_malloc(len);
  imgLen = imgBuf ? len : 0;
  if (imgBuf) memcpy(imgBuf, jpg, len);
  showStoredImage();
}

bool hasImage() { return imgLen > 0; }

void showStoredImage() {
  if (!imgLen) return;
  canvas.fillScreen(TFT_BLACK);
  canvas.drawJpg(imgBuf, imgLen, 0, 0, W, H);
  push();
}

void showPicker(const std::vector<String>& names, int selected, int current) {
  canvas.fillScreen(TFT_BLACK);
  drawBar();
  canvas.setFont(kBody);
  canvas.setTextDatum(top_left);
  int per = linesPerPage();
  int first = max(0, min(selected - per / 2, (int)names.size() - per));
  for (int i = 0; i < per && first + i < (int)names.size(); ++i) {
    int idx = first + i;
    int y = BAR + PAD + i * kLine;
    if (idx == selected) canvas.fillRect(0, y - 1, W, kLine, 0x3186);
    canvas.setTextColor(idx == current ? TFT_GREEN : TFT_WHITE);
    canvas.drawString((idx == current ? "● " : "  ") + names[idx], PAD, y);
  }
  push();
}

}  // namespace ui
