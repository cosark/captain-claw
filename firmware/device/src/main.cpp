// Captain Claw voice client for the M5Stack StickS3 and StopWatch (board.h).
//
//   Button A  (StickS3 front, StopWatch yellow)
//             hold = talk, release = send · tap = stop speaking / flip picture↔text
//             in the menu: choose / change the row
//   Button B  (StickS3 side, StopWatch the other one)
//             click = next page / next row · hold = menu (hold again = close)
//   Hold A while switching on (or no Wi-Fi saved) = setup hotspot
//
// Talks to Flight Deck's /device/ws (captain_claw/flight_deck/device_bridge.py):
// 16 kHz PCM up; reply text, 24 kHz PCM speech and JPEG pictures down.

#include <ArduinoJson.h>
#include <M5Unified.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <driver/gpio.h>
#include <esp_sleep.h>
#include <esp_timer.h>

#include "audio.h"
#include "board.h"
#include "face.h"
#include "ota.h"
#include "portal.h"
#include "settings.h"
#include "ui.h"

#define FW_VERSION "0.8.1"

enum class Mode { Idle, Listening, Waiting, Reply, Image, Menu, Agents, CharPick, Status, Notice };

static WebSocketsClient ws;
static WiFiMulti wifiMulti;
static bool wsUp = false;
static bool agentLinked = false;  // Flight Deck's link to the agent is up
static ota::Offer offer;           // a newer build Flight Deck offers (docs/device-ota-plan.md)
static String offerAnnounced;      // version the user has been told about
static bool offerAutoTried = false;
static bool probation = false;     // this boot is a fresh update that hasn't reached the deck yet
static bool checkingUpdate = false;
static Mode mode = Mode::Idle;
static String agentName, agentSlug;
static String heard, statusLine;
static std::vector<String> activity;  // what the agent is doing, oldest first
static int toolSteps = 0;
static uint32_t workStartMs = 0;
static bool workDirty = false;
static uint32_t recStartMs = 0, lastActivityMs = 0, lastDrawMs = 0, menuOpenedMs = 0;
static bool dimmed = false, screenOff = false;
static int menuSel = 0;
static std::vector<String> agentNames, agentSlugs;
static int agentSel = 0, agentCur = -1;
static bool agentsLoading = false;
static uint8_t charSel = 0;  // character being previewed
static int statusPage = 0, statusPages = 1;
static uint32_t flashStartMs = 0;
static bool flashing = false;
static int rttMs = -1;
static uint32_t lastPingMs = 0, lastReplyMs = 0, turns = 0;
static uint8_t* imgPart = nullptr;  // a picture arriving in parts (PSRAM)
static size_t imgPartLen = 0;
static uint32_t buzzOffMs = 0;       // vibration motor runs until then (0 = off)

static constexpr uint32_t kTapMs = 300;      // shorter press = tap, not talk
static constexpr uint32_t kDimLeadMs = 2000;  // dims this long before the screen goes off
static constexpr uint8_t kBright = 128;
static constexpr uint32_t kFlashMs = 700;    // white / normal, every 100 ms
static constexpr uint16_t kGreen = ui::rgb(40, 200, 90), kRed = ui::rgb(230, 50, 50);
static constexpr uint16_t kYellow = ui::rgb(250, 200, 40), kCyan = ui::rgb(60, 210, 240);
static constexpr uint16_t kOrange = ui::rgb(255, 150, 40), kText = ui::rgb(230, 230, 230);
static constexpr size_t kMaxImage = 256 * 1024;

// ── helpers ──────────────────────────────────────────────────────────

static String urlEncode(const String& s) {
  String o;
  const char* hex = "0123456789ABCDEF";
  for (uint8_t c : s) {
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
      o += (char)c;
    } else {
      o += '%';
      o += hex[c >> 4];
      o += hex[c & 15];
    }
  }
  return o;
}

static void touch() {
  lastActivityMs = millis();
  if (dimmed || screenOff) {
    ui::brightness(kBright);
    dimmed = screenOff = false;
  }
}

// A short buzz of the vibration motor (StopWatch); the loop switches it off.
static void buzz(uint16_t ms, uint8_t level = 200) {
  if (!board::kHasMotor) return;
  M5.Power.setVibration(level);
  buzzOffMs = millis() + ms;
  if (!buzzOffMs) buzzOffMs = 1;
}

static void sendJson(const JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
}

static void sendType(const char* type) {
  JsonDocument d;
  d["type"] = type;
  sendJson(d);
}

static void sendPrefs() {
  JsonDocument d;
  d["type"] = "prefs";
  d["voice"] = settings.femaleVoice ? "female" : "male";
  d["speak"] = settings.speak;
  sendJson(d);
}

static String serverHost() {
  String h = settings.server;
  int p = h.indexOf("://");
  if (p >= 0) h = h.substring(p + 3);
  int slash = h.indexOf('/');
  return slash < 0 ? h : h.substring(0, slash);
}

static String uptime() {
  uint32_t m = millis() / 60000;
  return m >= 60 ? String(m / 60) + "h " + String(m % 60) + "m" : String(m) + "m";
}

static ui::Health wifiHealth() {
  if (WiFi.status() != WL_CONNECTED) return ui::Down;
  return WiFi.RSSI() < -80 ? ui::Partial : ui::Up;
}

static ui::Health agentHealth() {
  if (!wsUp) return ui::Down;
  return agentLinked ? ui::Up : ui::Partial;
}

// "12s · 3 steps" while the agent runs.
static const char* workLabel() {
  static char buf[32];
  uint32_t secs = (millis() - workStartMs) / 1000;
  if (toolSteps) {
    snprintf(buf, sizeof(buf), "%lus · %d step%s", (unsigned long)secs, toolSteps, toolSteps == 1 ? "" : "s");
  } else {
    snprintf(buf, sizeof(buf), "working %lus", (unsigned long)secs);
  }
  return buf;
}

static void startWork() {
  activity.clear();
  toolSteps = 0;
  workStartMs = millis();
}

static void setBarState() {
  ui::setHealth(wifiHealth(), agentHealth());
  const char* st;
  uint16_t col;
  if (!wsUp) {
    st = "offline", col = kRed;
  } else if (mode == Mode::Listening) {
    st = "listening", col = kRed;
  } else if (audio::playing()) {
    st = "speaking", col = kCyan;
  } else if (mode == Mode::Waiting) {
    st = workLabel(), col = kYellow;
  } else if (mode == Mode::CharPick) {
    static char buf[24];
    snprintf(buf, sizeof(buf), "%d/%d  A=pick", charSel + 1, (int)face::KindCount);
    ui::setBar(face::name(charSel), buf, kCyan);
    return;
  } else if (mode == Mode::Menu || mode == Mode::Agents) {
    st = "menu", col = kText;
  } else if (mode == Mode::Status) {
    static char buf[16];
    if (statusPages > 1) snprintf(buf, sizeof(buf), "status %d/%d", statusPage + 1, statusPages);
    else snprintf(buf, sizeof(buf), "status");
    st = buf, col = kText;
  } else {
    st = agentLinked ? "ready" : "no agent", col = agentLinked ? kGreen : kYellow;
  }
  ui::setBar(agentName, st, col);
}

// ── status page / idle panel ─────────────────────────────────────────

static std::vector<ui::Row> statusRows() {
  std::vector<ui::Row> rows;
  bool wifiOk = WiFi.status() == WL_CONNECTED;
  rows.push_back({"Wi-Fi", wifiOk ? WiFi.SSID() + "  " + String(WiFi.RSSI()) + " dBm" : String("not connected"),
                  wifiOk ? (WiFi.RSSI() < -80 ? kYellow : kGreen) : kRed});
  rows.push_back({"IP", wifiOk ? WiFi.localIP().toString() : String("-"), kText});
  rows.push_back({"Deck", serverHost() + (wsUp ? (rttMs >= 0 ? "  " + String(rttMs) + " ms" : String("  ok")) : String("  offline")),
                  wsUp ? kGreen : kRed});
  rows.push_back({"Agent", (agentName.length() ? agentName : String("none")) + (agentLinked ? "" : "  (waiting)"),
                  agentLinked ? kGreen : (wsUp ? kYellow : kRed)});
  int pct = M5.Power.getBatteryLevel();
  int mv = M5.Power.getBatteryVoltage();
  bool chg = M5.Power.isCharging() == m5::Power_Class::is_charging;
  rows.push_back({"Battery", String(pct) + "%  " + String(mv / 1000.0f, 2) + " V" + (chg ? "  charging" : ""),
                  pct >= 40 ? kGreen : pct >= 20 ? kYellow : kRed});
  rows.push_back({"Voice", String(settings.femaleVoice ? "female" : "male") + "  vol " +
                               String((settings.volume * 100 + 127) / 255) + "%" +
                               (settings.boostDb ? "  +" + String(settings.boostDb) + " dB" : String("")) +
                               (settings.speak ? "" : "  muted"),
                  kText});
  rows.push_back({"Usage", String(turns) + " turn" + (turns == 1 ? "" : "s") +
                               (lastReplyMs ? "  last " + String((millis() - lastReplyMs) / 60000) + "m ago" : String("")) +
                               "  up " + uptime(),
                  kText});
  rows.push_back({"Device", deviceId() + "  fw " FW_VERSION "  " + String(ESP.getFreePsram() / 1024) + "k free", kText});
  if (offer.valid()) rows.push_back({"Update", offer.version + " available (menu → Software update)", kCyan});
  return rows;
}

static void showIdle() {
  if (settings.iconMode) {
    ui::showFace(settings.character, agentLinked ? face::Pose::Idle : face::Pose::Sad, 0);
    return;
  }
  bool wifiOk = WiFi.status() == WL_CONNECTED;
  bool ok = wsUp && agentLinked && wifiOk;
  ui::Home home;
  home.ready = ok;
  home.title = ok ? "All good" : "Getting ready";
  home.color = ok ? kGreen : kYellow;
  home.hint = ok        ? String("Hold the ") + board::kTalkButton + " to talk"
              : !wifiOk ? String("No Wi-Fi")
              : !wsUp   ? String("Connecting to Flight Deck…")
                        : String("Waiting for the agent");
  String noun = board::kNoun;
  noun.setCharAt(0, toupper(noun[0]));
  home.rows.push_back({noun, ok ? String("ready — hold ") + board::kTalkButton : String("not ready"), ok ? kGreen : kYellow});
  home.rows.push_back({"Wi-Fi", wifiOk ? WiFi.SSID() : String("not connected"), wifiOk ? kGreen : kRed});
  home.rows.push_back({"Deck", wsUp ? serverHost() : String("connecting…"), wsUp ? kGreen : kRed});
  home.rows.push_back({"Agent", agentLinked ? agentName : (wsUp ? String("waiting for agent") : String("-")),
                       agentLinked ? kGreen : kYellow});
  ui::showHome(home);
}

// ── menu ─────────────────────────────────────────────────────────────

enum MenuRow { MStatus, MAgent, MVolume, MBoost, MVoice, MDisplay, MTextSize, MCharacter, MBeep, MFlash, MBuzz,
               MLead, MScreenOff, MPowerOff, MUpdate, MAutoUpdate, MSetup, MCount };

// Rows this device has (only a device with a motor can buzz).
static bool rowShown(int row) { return row != MBuzz || board::kHasMotor; }

static constexpr uint8_t kVolumeSteps[] = {64, 128, 191, 255};  // 25 / 50 / 75 / 100 %
static constexpr uint8_t kBoostSteps[] = {0, 6, 10};

static String menuLabel(int row) {
  switch (row) {
    case MStatus: return "Status";
    case MAgent: return "Agent: " + (agentName.length() ? agentName : String("none")) + " »";
    case MVolume: return "Volume " + String((settings.volume * 100 + 127) / 255) + "%";
    case MBoost: return settings.boostDb ? "Boost +" + String(settings.boostDb) + " dB" : String("Boost off");
    case MVoice: return String("Voice: ") + (settings.femaleVoice ? "female" : "male");
    case MDisplay: return String("Display: ") + (settings.iconMode ? "icons" : "text");
    case MTextSize: {
      static const char* sizes[] = {"small", "medium", "large"};
      return String("Text size: ") + sizes[settings.textSize];
    }
    case MCharacter: return String("Character: ") + face::name(settings.character) + " »";
    case MBeep: return String("Beep before speech: ") + (settings.cueBeep ? "on" : "off");
    case MFlash: return String("Flash before speech: ") + (settings.cueFlash ? "on" : "off");
    case MBuzz: return String("Buzz before speech: ") + (settings.cueBuzz ? "on" : "off");
    case MLead: return "Cue lead: " + String(settings.cueLeadS) + " s";
    case MScreenOff: return "Screen off: " + String(settings.screenOffS) + " s";
    case MPowerOff: return "Power off: " + String(settings.powerOffS) + " s";
    case MUpdate: return offer.valid() ? "Update to " + offer.version + " »" : String("Software " FW_VERSION " · check");
    case MAutoUpdate: return String("Auto update: ") + (settings.autoUpdate ? "on" : "off");
    default: return "Wi-Fi & settings…";
  }
}

// A menu row as a card: the round screen shows the value big in a badge.
static ui::Item menuItem(int row) {
  static const char* sizes[] = {"small", "medium", "large"};
  auto onOff = [](bool on) { return String(on ? "on" : "off"); };
  ui::Item it;
  it.label = menuLabel(row);
  it.note = "yellow = change";
  switch (row) {
    case MStatus: it.title = "Status"; it.glyph = ui::Glyph::Info; it.note = "Wi-Fi, deck, battery…"; break;
    case MAgent:
      it.title = "Agent";
      it.badge = agentName.length() ? agentName : String("?");
      it.glyph = agentName.length() ? ui::Glyph::Initials : ui::Glyph::None;
      it.note = agentName.length() ? agentName + " · change" : String("none · choose one");
      break;
    case MVolume: it.title = "Volume"; it.badge = String((settings.volume * 100 + 127) / 255) + "%"; break;
    case MBoost:
      it.title = "Speech boost";
      it.badge = settings.boostDb ? "+" + String(settings.boostDb) + " dB" : String("off");
      it.on = settings.boostDb > 0;
      break;
    case MVoice: it.title = "Voice"; it.badge = settings.femaleVoice ? "female" : "male"; break;
    case MDisplay: it.title = "Display"; it.badge = settings.iconMode ? "icons" : "text"; break;
    case MTextSize:
      it.title = "Text size";
      it.badge = String("Aa");
      it.note = String(sizes[settings.textSize]) + " · change";
      break;
    case MCharacter: it.title = "Character"; it.badge = face::name(settings.character); it.note = "yellow = browse"; break;
    case MBeep: it.title = "Beep before speech"; it.badge = onOff(settings.cueBeep); it.on = settings.cueBeep; break;
    case MFlash: it.title = "Flash before speech"; it.badge = onOff(settings.cueFlash); it.on = settings.cueFlash; break;
    case MBuzz: it.title = "Buzz before speech"; it.badge = onOff(settings.cueBuzz); it.on = settings.cueBuzz; break;
    case MLead: it.title = "Cue lead"; it.badge = String(settings.cueLeadS) + " s"; it.note = "how early the cue comes"; break;
    case MScreenOff: it.title = "Screen off"; it.badge = String(settings.screenOffS) + " s"; it.note = "after this long idle"; break;
    case MPowerOff: it.title = "Power off"; it.badge = String(settings.powerOffS) + " s"; it.note = "after this long idle"; break;
    case MUpdate:
      it.title = "Software update";
      it.badge = offer.valid() ? offer.version : String(FW_VERSION);
      it.note = offer.valid() ? String("yellow = install") : String("up to date · yellow = check");
      break;
    case MAutoUpdate:
      it.title = "Auto update";
      it.badge = onOff(settings.autoUpdate);
      it.on = settings.autoUpdate;
      it.note = "installs when idle on USB";
      break;
    default: it.title = "Wi-Fi & settings"; it.glyph = ui::Glyph::Wifi; it.note = "opens the setup hotspot"; break;
  }
  return it;
}

// The rows this device shows, and where the selected one is among them.
static std::vector<ui::Item> menuItems(int* shownSel) {
  std::vector<ui::Item> items;
  for (int i = 0; i < MCount; ++i) {
    if (!rowShown(i)) continue;
    if (i == menuSel) *shownSel = items.size();
    items.push_back(menuItem(i));
  }
  return items;
}

static std::vector<ui::Item> agentItems() {
  std::vector<ui::Item> items;
  for (size_t i = 0; i < agentNames.size(); ++i) {
    ui::Item it;
    it.label = it.title = agentNames[i];
    bool back = !agentsLoading && i + 1 == agentNames.size();
    if (agentsLoading) {
      it.badge = "…";
    } else if (back) {
      it.title = "Back";
      it.glyph = ui::Glyph::Back;
      it.note = "to the menu";
    } else {
      it.badge = agentNames[i];
      it.glyph = ui::Glyph::Initials;
      it.note = (int)i == agentCur ? "current agent" : "yellow = choose";
    }
    items.push_back(it);
  }
  return items;
}

template <size_t N>
static uint8_t nextStep(const uint8_t (&steps)[N], uint8_t now) {
  for (uint8_t v : steps) {
    if (v > now) return v;
  }
  return steps[0];
}

static void backToMain();
static void startInstall();
static void connectServer();

static void redraw() {
  setBarState();
  switch (mode) {
    case Mode::Listening:
      if (settings.iconMode) ui::showFace(settings.character, face::Pose::Listening, 0);
      else ui::showListening(0, millis() - recStartMs);
      break;
    case Mode::Waiting:
      if (settings.iconMode) ui::showFace(settings.character, face::Pose::Thinking, 0);
      else ui::showWorking(heard, activity, statusLine, millis() - workStartMs, toolSteps);
      break;
    case Mode::Reply:
      if (settings.iconMode && audio::playing()) ui::showFace(settings.character, face::Pose::Speaking, audio::outputLevel());
      else ui::showReply();
      break;
    case Mode::Image: ui::showStoredImage(); break;
    case Mode::Menu: {
      int sel = 0;
      auto items = menuItems(&sel);
      ui::showPicker(items, sel, -1);
      break;
    }
    case Mode::Agents: ui::showPicker(agentItems(), agentSel, agentCur); break;
    case Mode::CharPick: {
      // Cycle through the poses so the preview shows everything it does.
      static const face::Pose poses[] = {face::Pose::Idle, face::Pose::Listening, face::Pose::Thinking,
                                         face::Pose::Speaking, face::Pose::Sad};
      uint32_t t = millis();
      face::Pose pose = poses[(t / 2500) % 5];
      int lvl = pose == face::Pose::Speaking || pose == face::Pose::Listening
                    ? 45 + (int)(40 * sinf(t / 90.0f)) : 0;
      ui::showCharPick(charSel, pose, lvl, charSel);
      break;
    }
    case Mode::Status: {
      auto rows = statusRows();
      int n = ui::showPanel("", kText, rows, statusPage);
      if (n != statusPages || statusPage >= n) {  // the bar showed a stale "x/y"
        statusPages = n;
        statusPage = min(statusPage, n - 1);
        setBarState();
        ui::showPanel("", kText, rows, statusPage);
      }
      break;
    }
    case Mode::Notice: break;  // stays until the next event
    case Mode::Idle:
      if (ui::hasReply()) ui::showReply();
      else showIdle();
      break;
  }
}

static void notice(const char* title, const String& body, uint16_t color) {
  mode = Mode::Notice;
  setBarState();
  ui::showMessage(title, body, color);
}

static void backToMain() {
  mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
  redraw();
}

static void openMenu() {
  menuSel = 0;
  menuOpenedMs = millis();
  mode = Mode::Menu;
  redraw();
}

static void chooseMenuRow() {
  menuOpenedMs = millis();
  switch (menuSel) {
    case MStatus:
      statusPage = 0;
      mode = Mode::Status;
      redraw();
      return;
    case MAgent:
      if (!wsUp) {
        notice("Offline", "Can't list agents until the stick is connected.", kRed);
        return;
      }
      agentNames = {"Loading agents…"};
      agentSlugs = {""};
      agentSel = 0;
      agentCur = -1;
      agentsLoading = true;
      mode = Mode::Agents;
      sendType("agents");
      redraw();
      return;
    case MSetup:
      runSetupPortal("Settings");
    case MVolume:
      settings.volume = nextStep(kVolumeSteps, settings.volume);
      audio::setVolume(settings.volume);
      break;
    case MBoost:
      settings.boostDb = nextStep(kBoostSteps, settings.boostDb);
      audio::setBoost(settings.boostDb);
      break;
    case MVoice:
      settings.femaleVoice = !settings.femaleVoice;
      if (wsUp) sendPrefs();
      break;
    case MDisplay: settings.iconMode = !settings.iconMode; break;
    case MTextSize:
      settings.textSize = (settings.textSize + 1) % 3;
      ui::setTextSize(settings.textSize);
      break;
    case MCharacter:
      charSel = settings.character;
      mode = Mode::CharPick;
      redraw();
      return;
    case MBeep: settings.cueBeep = !settings.cueBeep; break;
    case MFlash: settings.cueFlash = !settings.cueFlash; break;
    case MBuzz:
      settings.cueBuzz = !settings.cueBuzz;
      if (settings.cueBuzz) buzz(160);
      break;
    case MLead: settings.cueLeadS = settings.cueLeadS == 1 ? 2 : 1; break;
    case MScreenOff: settings.screenOffS = settings.screenOffS == 5 ? 10 : settings.screenOffS == 10 ? 15 : 5; break;
    case MPowerOff: settings.powerOffS = settings.powerOffS == 15 ? 30 : settings.powerOffS == 30 ? 60 : 15; break;
    case MUpdate:
      if (offer.valid()) {
        startInstall();
      } else if (wsUp) {
        checkingUpdate = true;
        sendType("update_check");
        notice("Software update", "Checking for a newer version…", kText);
      } else {
        notice("Offline", "Connect to Flight Deck to check for updates.", kRed);
      }
      return;
    case MAutoUpdate: settings.autoUpdate = !settings.autoUpdate; break;
  }
  settings.save();
  redraw();
  if (menuSel == MVolume || menuSel == MBoost) audio::beep();
  if (menuSel == MBeep && settings.cueBeep) audio::cue();
}

static void chooseAgent() {
  if (agentsLoading) return;
  if (agentSel == (int)agentNames.size() - 1) {  // "« Back"
    mode = Mode::Menu;
    redraw();
    return;
  }
  settings.agent = agentSlugs[agentSel];
  settings.save();
  JsonDocument d;
  d["type"] = "select";
  d["agent"] = settings.agent;
  sendJson(d);
  backToMain();
}

// ── microphone → server ──────────────────────────────────────────────

static void micSink(const int16_t* samples, size_t count) {
  if (wsUp) ws.sendBIN((const uint8_t*)samples, count * sizeof(int16_t));
}

static void startTalking() {
  if (!wsUp) {
    notice("Offline", "Not connected to Flight Deck yet.", kRed);
    return;
  }
  flashing = false;
  audio::micStart();  // also stops any playback
  JsonDocument d;
  d["type"] = "rec_start";
  d["rate"] = audio::kMicRate;
  sendJson(d);
  recStartMs = millis();
  mode = Mode::Listening;
  buzz(35);
  redraw();
}

static void stopTalking(bool send) {
  audio::micStop(send ? micSink : [](const int16_t*, size_t) {});
  sendType(send ? "rec_end" : "rec_cancel");
  if (send) {
    heard = "";
    statusLine = "Transcribing…";
    startWork();
    mode = Mode::Waiting;
    redraw();
  } else {
    backToMain();
  }
}

// ── speech cue: beep + flash, then the speech ────────────────────────

static void startSpeech(uint32_t rate) {
  bool cue = settings.cueBeep || settings.cueFlash;
  bool buzzCue = board::kHasMotor && settings.cueBuzz;
  audio::playBegin(rate, (cue || buzzCue) ? settings.cueLeadS * 1000UL : 0);
  touch();
  if (settings.cueBeep) audio::cue();
  if (buzzCue) buzz(160);
  if (settings.cueFlash) {
    M5.Display.setBrightness(255);
    flashing = true;
    flashStartMs = millis();
  }
}

static void pollFlash() {
  if (!flashing) return;
  uint32_t el = millis() - flashStartMs;
  static int lastPhase = -1;
  int phase = el / 100;
  if (el >= kFlashMs) {
    flashing = false;
    lastPhase = -1;
    ui::brightness(kBright);
    redraw();
    return;
  }
  if (phase != lastPhase) {
    lastPhase = phase;
    if (phase % 2 == 0) ui::flashFrame(true);
    else redraw();
  }
}

// ── server → device ──────────────────────────────────────────────────

static void onText(const char* payload, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  String type = doc["type"] | "";

  if (type == "bound") {
    agentName = doc["agent"] | "";
    agentSlug = doc["slug"] | "";
    agentLinked = doc["linked"] | true;
    if (mode == Mode::Agents || mode == Mode::Idle || mode == Mode::Notice) backToMain();
    else redraw();
  } else if (type == "agents") {
    agentNames.clear();
    agentSlugs.clear();
    agentCur = -1;
    String current = doc["current"] | "";
    for (JsonObject a : doc["items"].as<JsonArray>()) {
      String slug = a["slug"] | "";
      if (slug == current) agentCur = agentNames.size();
      agentNames.push_back(a["name"] | slug);
      agentSlugs.push_back(slug);
    }
    agentNames.push_back("« Back");
    agentSlugs.push_back("");
    agentSel = max(0, agentCur);
    agentsLoading = false;
    if (mode == Mode::Agents) redraw();
  } else if (type == "state") {
    String st = doc["state"] | "";
    if (st == "connected") {
      agentLinked = true;
    } else if (st == "agent_offline") {
      agentLinked = false;
    } else if (st == "transcribing") {
      statusLine = "Transcribing…";
    } else if (st == "thinking") {
      statusLine = "Thinking…";
      if (mode != Mode::Waiting && mode != Mode::Listening) {
        startWork();  // a typed or resent turn: no recording came first
        mode = Mode::Waiting;
      }
    } else if (st == "ready" && mode == Mode::Waiting) {
      mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
    }
    if (mode != Mode::Listening && !flashing) redraw();
  } else if (type == "transcript") {
    heard = doc["text"] | "";
    statusLine = "Thinking…";
    workDirty = true;
  } else if (type == "status") {
    statusLine = doc["text"] | "";
    workDirty = true;
  } else if (type == "activity") {
    String line = doc["text"] | "";
    String tool = doc["tool"] | "";
    if (line.length()) {
      activity.push_back(line);
      if (activity.size() > 8) activity.erase(activity.begin());
      if (tool.length() && tool != "progress") ++toolSteps;
      workDirty = true;
    }
  } else if (type == "reply") {
    touch();
    ++turns;
    lastReplyMs = millis();
    String said = doc["text"] | "";
    ui::setReply(said);
    face::setText(said);
    mode = Mode::Reply;
    if (!flashing) redraw();
  } else if (type == "audio_start") {
    startSpeech(doc["rate"] | 24000);
    setBarState();
  } else if (type == "audio_end") {
    audio::playEnd();
  } else if (type == "update") {
    ota::Offer o;
    o.version = doc["version"] | "";
    o.size = doc["size"] | 0;
    o.sha256 = doc["sha256"] | "";
    o.sig = doc["sig"] | "";
    o.url = doc["url"] | "";
    o.notes = doc["notes"] | "";
    if (o.valid() && ota::compareVersions(o.version, FW_VERSION) > 0) {
      offer = o;
      bool asked = checkingUpdate;
      checkingUpdate = false;
      if ((asked || offerAnnounced != o.version) && !settings.autoUpdate &&
          (mode == Mode::Idle || mode == Mode::Reply || mode == Mode::Notice || mode == Mode::Menu)) {
        offerAnnounced = o.version;
        notice(("Update " + o.version + " available").c_str(),
               (o.notes.length() ? o.notes + " — " : String("")) + "Menu → Software update.", kCyan);
      }
    }
  } else if (type == "update_none") {
    if (checkingUpdate) {
      checkingUpdate = false;
      notice("Up to date", String("This is the latest version (" FW_VERSION ")."), kGreen);
    }
  } else if (type == "pong") {
    if (doc["t"].is<uint32_t>()) rttMs = millis() - doc["t"].as<uint32_t>();
  } else if (type == "error") {
    touch();
    buzz(250);
    notice("Hmm", doc["text"] | "Something went wrong.", kOrange);
  }
}

static void dropImagePart() {
  free(imgPart);
  imgPart = nullptr;
  imgPartLen = 0;
}

// A picture: 0x03 frames are parts with more to come, 0x02 is the last (or
// only) part. A WebSocket frame here carries at most ~15 KB.
static void imageData(const uint8_t* data, size_t len, bool last) {
  size_t need = imgPartLen + len;
  uint8_t* grown = need <= kMaxImage ? (uint8_t*)ps_realloc(imgPart, need) : nullptr;
  if (!grown) {
    dropImagePart();
    return;
  }
  memcpy(grown + imgPartLen, data, len);
  imgPart = grown;
  imgPartLen = need;
  if (!last) return;
  touch();
  ui::storeImage(imgPart, imgPartLen);  // the screen keeps the bytes
  imgPart = nullptr;
  imgPartLen = 0;
  if (!(settings.iconMode && audio::playing())) {
    mode = Mode::Image;
    if (!flashing) redraw();
  }
}

static void onBinary(const uint8_t* data, size_t len) {
  if (len < 2) return;
  if (data[0] == 0x01) {
    audio::playFeed(data + 1, len - 1);
  } else if (data[0] == 0x02 || data[0] == 0x03) {
    imageData(data + 1, len - 1, data[0] == 0x02);
  }
}

static void onWs(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      Serial.println("[claw] flight deck connected");
      wsUp = true;
      rttMs = -1;
      sendPrefs();
      redraw();
      break;
    case WStype_DISCONNECTED:
      if (wsUp) {
        Serial.println("[claw] flight deck disconnected");
        wsUp = false;
        agentLinked = false;
        if (audio::micActive()) audio::micStop([](const int16_t*, size_t) {});
        audio::playStop();
        dropImagePart();
        if (mode == Mode::Listening || mode == Mode::Waiting) mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
        redraw();
      }
      break;
    case WStype_TEXT:
      onText((const char*)payload, length);
      break;
    case WStype_BIN:
      onBinary(payload, length);
      break;
    default:
      break;
  }
}

static void connectServer() {
  String url = settings.server;
  bool tls = url.startsWith("wss://");
  url = url.substring(tls ? 6 : 5);
  int slash = url.indexOf('/');
  String hostport = slash < 0 ? url : url.substring(0, slash);
  String prefix = slash < 0 ? "" : url.substring(slash);
  int colon = hostport.lastIndexOf(':');
  String host = colon < 0 ? hostport : hostport.substring(0, colon);
  uint16_t port = colon < 0 ? (tls ? 443 : 80) : hostport.substring(colon + 1).toInt();
#ifdef OTA_TEST_NO_DECK
  // Rollback test build (PLATFORMIO_BUILD_FLAGS=-DOTA_TEST_NO_DECK): Wi-Fi works,
  // Flight Deck is never reached, so an update to this build must roll back.
  host = "deck.invalid";
#endif

  String path = prefix + "/device/ws?device=" + urlEncode(deviceId()) + "&sw=" + String(board::kScreenW) +
                "&sh=" + String(board::kScreenH);
  if (board::kRound) path += "&round=1";               // fit pictures inside the circle
  if (board::kChunkedImages) path += "&img=chunks";    // pictures bigger than one frame
  if (settings.token.length()) path += "&token=" + urlEncode(settings.token);
  if (settings.agent.length()) path += "&agent=" + urlEncode(settings.agent);
  if (settings.lang.length()) path += "&lang=" + urlEncode(settings.lang);
  path += String("&voice=") + (settings.femaleVoice ? "female" : "male");
  path += String("&hw=") + board::kHw + "&fw=" FW_VERSION;
  if (!settings.speak) path += "&speak=0";

  if (tls) {
    ws.beginSSL(host.c_str(), port, path.c_str(), "", "");  // no pinning (v1)
  } else {
    ws.begin(host.c_str(), port, path.c_str(), "");
  }
  // The library sends "Origin: file://" by default — Flight Deck's origin guard
  // (rightly) refuses that as a cross-site browser. A device sends no Origin.
  ws.setExtraHeaders("");
  ws.onEvent(onWs);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 5000, 2);
}

// ── over-the-air updates (docs/device-ota-plan.md) ───────────────────

static String httpBase() {
  String b = settings.server;
  if (b.startsWith("wss://")) b = "https://" + b.substring(6);
  else if (b.startsWith("ws://")) b = "http://" + b.substring(5);
  while (b.endsWith("/")) b.remove(b.length() - 1);
  return b;
}

static bool onUsbPower() {
  int vbus = M5.Power.getVBUSVoltage();
  if (vbus > 0) return vbus > 4000;
  return M5.Power.isCharging() == m5::Power_Class::is_charging || M5.Power.getBatteryLevel() >= 99;
}

static void installProgress(size_t done, size_t total) {
  static int lastPct = -1;
  int pct = total ? (int)(done * 100 / total) : 0;
  if (pct == lastPct || (pct % 2 && pct != 100)) return;
  lastPct = pct;
  ui::showMessage(("Updating to " + offer.version).c_str(),
                  String(pct) + "%  ·  " + String(done / 1024) + " / " + String(total / 1024) +
                      " KB. Keep it on.",
                  kCyan);
}

static void startInstall() {
  if (!offer.valid()) return;
  if (mode == Mode::Listening || mode == Mode::Waiting || audio::playing()) {
    notice("Not now", "Finish the conversation first.", kOrange);
    return;
  }
  int pct = M5.Power.getBatteryLevel();
  if (!onUsbPower() && pct >= 0 && pct < 30) {
    notice("Battery low", "Charge to 30% (or plug in) to update.", kOrange);
    return;
  }
  touch();
  audio::playStop();
  ws.disconnect();  // one TLS connection at a time; we restart anyway
  wsUp = false;
  agentLinked = false;
  ui::setHealth(wifiHealth(), ui::Partial);
  ui::setBar(agentName, "updating", kCyan);
  ui::showMessage(("Updating to " + offer.version).c_str(), String("Downloading…"), kCyan);
  String err = ota::install(offer, httpBase(), settings.token, installProgress);
  if (err.isEmpty()) {
    ui::showMessage("Update installed", "Restarting into " + offer.version + "…", kGreen);
    delay(1200);
    ESP.restart();
  }
  lastActivityMs = millis();
  notice("Update failed", err + ". Still on " FW_VERSION ".", kOrange);
  connectServer();
}

// ── power ────────────────────────────────────────────────────────────

static void goToSleep() {
  ui::showMessage("Sleeping", String("Press the ") + board::kTalkButton + " to wake.", TFT_DARKGREY);
  delay(1200);
  ws.disconnect();
  WiFi.disconnect(true);
  if (board::kHasMotor) M5.Power.setVibration(0);
  ui::brightness(0);
  M5.Display.sleep();
  esp_sleep_enable_ext0_wakeup(board::kTalkPin, 0);  // the talk button pulls low
  esp_deep_sleep_start();
}

// "Nothing happening" = no button, no new reply, and not listening, speaking,
// flashing or waiting for the agent: then the screen goes off, later the
// stick powers off (deep sleep; the front button wakes it).
static void handleIdlePower() {
  if (mode == Mode::Listening || mode == Mode::Waiting || audio::playing() || flashing || probation) {
    lastActivityMs = millis();
    return;
  }
  uint32_t idle = millis() - lastActivityMs;
  uint32_t offMs = settings.screenOffS * 1000UL;
  if (!dimmed && idle > offMs - min(offMs / 2, kDimLeadMs)) {
    ui::brightness(24);
    dimmed = true;
  }
  if (!screenOff && idle > offMs) {
    ui::brightness(0);
    screenOff = true;
  }
  if (idle > settings.powerOffS * 1000UL) goToSleep();
}

// Both buttons are sampled every 20 ms off the main loop, so any press (or a
// held button) counts as activity even when a loop pass is slow — drawing,
// TLS, saving settings — and a quick click would slip between two passes.
static volatile uint32_t buttonSeenMs = 0;

static void sampleButtons(void*) {
  if (gpio_get_level(board::kTalkPin) == 0 || gpio_get_level(board::kSidePin) == 0) buttonSeenMs = millis();
}

static void noteButtonActivity() {
  uint32_t seen = buttonSeenMs;
  if ((int32_t)(seen - lastActivityMs) > 0) {
    lastActivityMs = seen;
    if (dimmed && !screenOff) {  // brighten back; a dark screen wakes on the press itself
      ui::brightness(kBright);
      dimmed = false;
    }
  }
}

// ── setup / loop ─────────────────────────────────────────────────────

void setup() {
  auto cfg = M5.config();
  cfg.internal_mic = true;
  cfg.internal_spk = true;
  M5.begin(cfg);
  Serial.begin(115200);
  ui::begin();
  ui::brightness(kBright);
  settings.load();
  probation = ota::onProbation();
  ui::setTextSize(settings.textSize);
  Serial.printf("[claw] boot %s fw %s%s: %d network(s), server '%s'\n", deviceId().c_str(), FW_VERSION,
                probation ? " (on probation)" : "", settings.netCount, settings.server.c_str());
  audio::begin(settings.volume, settings.boostDb);
  M5.BtnB.setHoldThresh(800);
  esp_timer_create_args_t sampler = {};
  sampler.callback = sampleButtons;
  sampler.name = "buttons";
  esp_timer_handle_t samplerTimer;
  if (esp_timer_create(&sampler, &samplerTimer) == ESP_OK) esp_timer_start_periodic(samplerTimer, 20000);

  M5.update();
  if (M5.BtnA.isPressed() || !settings.configured()) {
    runSetupPortal(settings.configured() ? "Setup" : "First-time setup");
  }

  ui::setHealth(ui::Down, ui::Down);
  ui::setBar("", "wi-fi…", kYellow);
  ui::showMessage("Connecting to Wi-Fi", settings.nets[0].ssid, kText);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(deviceId().c_str());
  for (int i = 0; i < settings.netCount; ++i) {
    wifiMulti.addAP(settings.nets[i].ssid.c_str(), settings.nets[i].pass.c_str());
  }
  uint32_t t0 = millis();
  while (wifiMulti.run(5000) != WL_CONNECTED) {
    M5.update();
    // A fresh update that can't even reach Wi-Fi goes back to the old version
    // (which may well be fine) rather than into setup.
    if (probation && millis() - t0 > 30000) ota::rollBack();
    if (!probation && (M5.BtnA.wasPressed() || millis() - t0 > 30000)) {
      runSetupPortal("No Wi-Fi in reach");
    }
  }
  Serial.printf("[claw] wifi %s ip %s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  ui::setHealth(wifiHealth(), ui::Down);
  ui::showMessage("Wi-Fi connected", WiFi.SSID() + "  " + WiFi.localIP().toString(), kGreen);
  configTzTime(settings.tz.c_str(), "pool.ntp.org", "time.google.com");  // the round screen's clock
  connectServer();
  lastActivityMs = millis();
}

void loop() {
  M5.update();
  noteButtonActivity();
  ws.loop();
  if (WiFi.status() != WL_CONNECTED) wifiMulti.run(0);
  bool inMenu = mode == Mode::Menu || mode == Mode::Agents || mode == Mode::Status || mode == Mode::CharPick;
  // On a dark screen a press only wakes it — except the front button on the
  // main screen, which starts talking right away.
  bool dark = screenOff;

  // Front button: hold to talk, tap for quick actions; in the menu, choose.
  if (M5.BtnA.wasPressed()) {
    touch();
    if (dark && inMenu) {
      // woken only
    } else if (mode == Mode::Menu) {
      chooseMenuRow();
    } else if (mode == Mode::Agents) {
      chooseAgent();
    } else if (mode == Mode::CharPick) {
      settings.character = charSel;
      settings.save();
      mode = Mode::Menu;
      menuOpenedMs = millis();
      redraw();
    } else if (mode == Mode::Status) {
      openMenu();
    } else {
      bool wasSpeaking = audio::playing();
      startTalking();
      if (wasSpeaking) sendType("stop");
    }
  }
  if (M5.BtnA.wasReleased() && mode == Mode::Listening) {
    bool tap = millis() - recStartMs < kTapMs;
    stopTalking(!tap);
    if (tap) {
      sendType("stop");  // a tap also silences the rest of the answer
      if (ui::hasImage() && mode == Mode::Reply) {
        mode = Mode::Image;
        redraw();
      }
    }
  }

  // Side button: page / next row · hold = menu (hold again = close).
  if (dark && (M5.BtnB.wasHold() || M5.BtnB.wasClicked())) {
    touch();  // woken only
  } else if (M5.BtnB.wasHold()) {
    touch();
    if (mode == Mode::CharPick) {  // back to the menu, keeping the old character
      mode = Mode::Menu;
      menuOpenedMs = millis();
      redraw();
    } else if (inMenu) {
      backToMain();
    } else if (mode != Mode::Listening) {
      openMenu();
    }
  } else if (M5.BtnB.wasClicked()) {
    touch();
    if (mode == Mode::Menu) {
      do menuSel = (menuSel + 1) % MCount;
      while (!rowShown(menuSel));
      menuOpenedMs = millis();
      redraw();
    } else if (mode == Mode::CharPick) {
      charSel = (charSel + 1) % face::KindCount;
      menuOpenedMs = millis();
      redraw();
    } else if (mode == Mode::Agents) {
      if (!agentsLoading) agentSel = (agentSel + 1) % agentNames.size();
      menuOpenedMs = millis();
      redraw();
    } else if (mode == Mode::Status) {
      if (statusPage + 1 < statusPages) {
        ++statusPage;
        redraw();
      } else {
        backToMain();  // after the last page
      }
    } else if (mode == Mode::Image || mode == Mode::Notice) {
      backToMain();
    } else if (mode == Mode::Reply) {
      if (audio::playing() && settings.iconMode) {
        audio::playStop();  // the character gives way to the text
        sendType("stop");
        redraw();
      } else {
        ui::nextPage();
      }
    } else if (mode == Mode::Idle) {
      statusPage = 0;
      mode = Mode::Status;
      redraw();
    }
  }
  if ((mode == Mode::Menu || mode == Mode::Agents) && millis() - menuOpenedMs > 20000) backToMain();
  if (mode == Mode::CharPick && millis() - menuOpenedMs > 60000) backToMain();

  // Keep a round-trip time for the status page.
  if (wsUp && millis() - lastPingMs > 15000) {
    lastPingMs = millis();
    JsonDocument d;
    d["type"] = "ping";
    d["t"] = millis();
    sendJson(d);
  }

  // Keep the mic stream and the speaker fed.
  if (mode == Mode::Listening) {
    int level = audio::micPoll(micSink);
    if (millis() - lastDrawMs > 80) {
      lastDrawMs = millis();
      setBarState();
      if (settings.iconMode) ui::showFace(settings.character, face::Pose::Listening, level);
      else ui::showListening(level, millis() - recStartMs);
    }
    if (millis() - recStartMs > 110000) stopTalking(true);  // the server caps at 120 s
  }
  if (buzzOffMs && (int32_t)(millis() - buzzOffMs) >= 0) {
    M5.Power.setVibration(0);
    buzzOffMs = 0;
  }
  pollFlash();
  bool wasPlaying = audio::playing();
  audio::playPoll();
  if (wasPlaying != audio::playing() && (mode == Mode::Reply || mode == Mode::Idle) && !flashing) redraw();

  if (!flashing && !screenOff) {
    uint32_t since = millis() - lastDrawMs;
    if (mode == Mode::Waiting) {
      // Changes at most ~6×/s; the timer (and the icon's dots) once in a while.
      static uint32_t lastSecond = 0;
      uint32_t secs = (millis() - workStartMs) / 1000;
      if ((workDirty && since > 150) || secs != lastSecond || (settings.iconMode && since > face::frameMs(settings.character)) ||
          (ui::animated() && since > 60)) {
        lastSecond = secs;
        lastDrawMs = millis();
        workDirty = false;
        redraw();
      }
    } else if (settings.iconMode && mode == Mode::Reply && audio::playing() && since > min<uint32_t>(60, face::frameMs(settings.character))) {
      lastDrawMs = millis();
      setBarState();
      ui::showFace(settings.character, face::Pose::Speaking, audio::outputLevel());
    } else if (!settings.iconMode && ui::animated() && mode == Mode::Reply && audio::playing() && since > 60) {
      lastDrawMs = millis();  // the speaking ring follows the voice
      redraw();
    } else if (settings.iconMode && mode == Mode::Idle && !ui::hasReply() && since > face::frameMs(settings.character)) {
      lastDrawMs = millis();  // blinking, rain, bouncing…
      redraw();
    } else if (mode == Mode::CharPick && since > face::frameMs(charSel)) {
      lastDrawMs = millis();
      redraw();
    } else if ((mode == Mode::Status && since > 1000) || (ui::animated() && mode == Mode::Idle && since > 1000) ||
               since > 30000) {
      lastDrawMs = millis();  // live status page; battery/health in the bar elsewhere
      if (mode != Mode::Listening && mode != Mode::Notice && mode != Mode::Image) redraw();
    }
  }

  // A fresh update proves itself by reaching Flight Deck within 90 s;
  // otherwise the bootloader goes back to the previous version.
  if (probation) {
    if (wsUp) {
      ota::markGood();
      probation = false;
    } else if (millis() > 90000) {
      ota::rollBack();
    }
  }
  if (wsUp && settings.lastFw != FW_VERSION) {  // first run of this version
    bool updated = settings.lastFw.length() > 0;
    settings.lastFw = FW_VERSION;
    settings.save();
    if (updated && (mode == Mode::Idle || mode == Mode::Reply)) {
      notice("Updated", "Now running " FW_VERSION ".", kGreen);
    }
  }
  if (settings.autoUpdate && offer.valid() && !offerAutoTried && !audio::playing() &&
      (mode == Mode::Idle || mode == Mode::Reply || mode == Mode::Image) && onUsbPower()) {
    offerAutoTried = true;
    startInstall();
  }

  noteButtonActivity();
  handleIdlePower();
  delay(1);
}
