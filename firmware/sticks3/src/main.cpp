// Captain Claw voice client for the M5Stack StickS3.
//
//   Front button (A)  hold = talk, release = send · tap = stop speaking / flip picture↔text
//                     in the menu: choose / change the row
//   Side button  (B)  click = next page / next row · hold = menu (hold again = close)
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
#include "face.h"
#include "portal.h"
#include "settings.h"
#include "ui.h"

#define FW_VERSION "0.6.4"

enum class Mode { Idle, Listening, Waiting, Reply, Image, Menu, Agents, CharPick, Status, Notice };

static WebSocketsClient ws;
static WiFiMulti wifiMulti;
static bool wsUp = false;
static bool agentLinked = false;  // Flight Deck's link to the agent is up
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

static constexpr uint32_t kTapMs = 300;      // shorter press = tap, not talk
static constexpr uint32_t kDimLeadMs = 2000;  // dims this long before the screen goes off
static constexpr uint8_t kBright = 128;
static constexpr uint32_t kFlashMs = 700;    // white / normal, every 100 ms
static constexpr uint16_t kGreen = ui::rgb(40, 200, 90), kRed = ui::rgb(230, 50, 50);
static constexpr uint16_t kYellow = ui::rgb(250, 200, 40), kCyan = ui::rgb(60, 210, 240);
static constexpr uint16_t kOrange = ui::rgb(255, 150, 40), kText = ui::rgb(230, 230, 230);

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
  return rows;
}

static void showIdle() {
  if (settings.iconMode) {
    ui::showFace(settings.character, agentLinked ? face::Pose::Idle : face::Pose::Sad, 0);
    return;
  }
  bool ok = wsUp && agentLinked && WiFi.status() == WL_CONNECTED;
  std::vector<ui::Row> rows;
  rows.push_back({"Stick", ok ? String("ready — hold front button") : String("not ready"), ok ? kGreen : kYellow});
  bool wifiOk = WiFi.status() == WL_CONNECTED;
  rows.push_back({"Wi-Fi", wifiOk ? WiFi.SSID() : String("not connected"), wifiOk ? kGreen : kRed});
  rows.push_back({"Deck", wsUp ? serverHost() : String("connecting…"), wsUp ? kGreen : kRed});
  rows.push_back({"Agent", agentLinked ? agentName : (wsUp ? String("waiting for agent") : String("-")),
                  agentLinked ? kGreen : kYellow});
  ui::showPanel(ok ? "All good" : "Getting ready", ok ? kGreen : kYellow, rows);
}

// ── menu ─────────────────────────────────────────────────────────────

enum MenuRow { MStatus, MAgent, MVolume, MBoost, MVoice, MDisplay, MTextSize, MCharacter, MBeep, MFlash, MLead,
               MScreenOff, MPowerOff, MSetup, MCount };

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
    case MLead: return "Cue lead: " + String(settings.cueLeadS) + " s";
    case MScreenOff: return "Screen off: " + String(settings.screenOffS) + " s";
    case MPowerOff: return "Power off: " + String(settings.powerOffS) + " s";
    default: return "Wi-Fi & settings…";
  }
}

static std::vector<String> menuNames() {
  std::vector<String> names;
  for (int i = 0; i < MCount; ++i) names.push_back(menuLabel(i));
  return names;
}

template <size_t N>
static uint8_t nextStep(const uint8_t (&steps)[N], uint8_t now) {
  for (uint8_t v : steps) {
    if (v > now) return v;
  }
  return steps[0];
}

static void backToMain();

static void redraw() {
  setBarState();
  switch (mode) {
    case Mode::Listening:
      if (settings.iconMode) ui::showFace(settings.character, face::Pose::Listening, 0);
      else ui::showListening(0, millis() - recStartMs);
      break;
    case Mode::Waiting:
      if (settings.iconMode) ui::showFace(settings.character, face::Pose::Thinking, 0);
      else ui::showWorking(heard, activity, statusLine);
      break;
    case Mode::Reply:
      if (settings.iconMode && audio::playing()) ui::showFace(settings.character, face::Pose::Speaking, audio::outputLevel());
      else ui::showReply();
      break;
    case Mode::Image: ui::showStoredImage(); break;
    case Mode::Menu: ui::showPicker(menuNames(), menuSel, -1); break;
    case Mode::Agents: ui::showPicker(agentNames, agentSel, agentCur); break;
    case Mode::CharPick: {
      // Cycle through the poses so the preview shows everything it does.
      static const face::Pose poses[] = {face::Pose::Idle, face::Pose::Listening, face::Pose::Thinking,
                                         face::Pose::Speaking, face::Pose::Sad};
      uint32_t t = millis();
      face::Pose pose = poses[(t / 2500) % 5];
      int lvl = pose == face::Pose::Speaking || pose == face::Pose::Listening
                    ? 45 + (int)(40 * sinf(t / 90.0f)) : 0;
      ui::showFace(charSel, pose, lvl);
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
    case MLead: settings.cueLeadS = settings.cueLeadS == 1 ? 2 : 1; break;
    case MScreenOff: settings.screenOffS = settings.screenOffS == 5 ? 10 : settings.screenOffS == 10 ? 15 : 5; break;
    case MPowerOff: settings.powerOffS = settings.powerOffS == 15 ? 30 : settings.powerOffS == 30 ? 60 : 15; break;
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
  audio::playBegin(rate, cue ? settings.cueLeadS * 1000UL : 0);
  touch();
  if (settings.cueBeep) audio::cue();
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
  } else if (type == "pong") {
    if (doc["t"].is<uint32_t>()) rttMs = millis() - doc["t"].as<uint32_t>();
  } else if (type == "error") {
    touch();
    notice("Hmm", doc["text"] | "Something went wrong.", kOrange);
  }
}

static void onBinary(const uint8_t* data, size_t len) {
  if (len < 2) return;
  if (data[0] == 0x01) {
    audio::playFeed(data + 1, len - 1);
  } else if (data[0] == 0x02) {
    touch();
    ui::storeImage(data + 1, len - 1);
    if (!(settings.iconMode && audio::playing())) {
      mode = Mode::Image;
      if (!flashing) redraw();
    }
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

  String path = prefix + "/device/ws?device=" + urlEncode(deviceId()) + "&sw=240&sh=135";
  if (settings.token.length()) path += "&token=" + urlEncode(settings.token);
  if (settings.agent.length()) path += "&agent=" + urlEncode(settings.agent);
  if (settings.lang.length()) path += "&lang=" + urlEncode(settings.lang);
  path += String("&voice=") + (settings.femaleVoice ? "female" : "male");
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

// ── power ────────────────────────────────────────────────────────────

static void goToSleep() {
  ui::showMessage("Sleeping", "Press the front button to wake.", TFT_DARKGREY);
  delay(1200);
  ws.disconnect();
  WiFi.disconnect(true);
  ui::brightness(0);
  M5.Display.sleep();
  esp_sleep_enable_ext0_wakeup(GPIO_NUM_11, 0);  // front button pulls low
  esp_deep_sleep_start();
}

// "Nothing happening" = no button, no new reply, and not listening, speaking,
// flashing or waiting for the agent: then the screen goes off, later the
// stick powers off (deep sleep; the front button wakes it).
static void handleIdlePower() {
  if (mode == Mode::Listening || mode == Mode::Waiting || audio::playing() || flashing) {
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
  if (gpio_get_level(GPIO_NUM_11) == 0 || gpio_get_level(GPIO_NUM_12) == 0) buttonSeenMs = millis();
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
  ui::setTextSize(settings.textSize);
  Serial.printf("[claw] boot %s fw %s: %d network(s), server '%s'\n", deviceId().c_str(), FW_VERSION,
                settings.netCount, settings.server.c_str());
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
    if (M5.BtnA.wasPressed() || millis() - t0 > 30000) {
      runSetupPortal("No Wi-Fi in reach");
    }
  }
  Serial.printf("[claw] wifi %s ip %s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  ui::setHealth(wifiHealth(), ui::Down);
  ui::showMessage("Wi-Fi connected", WiFi.SSID() + "  " + WiFi.localIP().toString(), kGreen);
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
      menuSel = (menuSel + 1) % MCount;
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
      if ((workDirty && since > 150) || secs != lastSecond || (settings.iconMode && since > face::frameMs(settings.character))) {
        lastSecond = secs;
        lastDrawMs = millis();
        workDirty = false;
        redraw();
      }
    } else if (settings.iconMode && mode == Mode::Reply && audio::playing() && since > min<uint32_t>(60, face::frameMs(settings.character))) {
      lastDrawMs = millis();
      setBarState();
      ui::showFace(settings.character, face::Pose::Speaking, audio::outputLevel());
    } else if (settings.iconMode && mode == Mode::Idle && !ui::hasReply() && since > face::frameMs(settings.character)) {
      lastDrawMs = millis();  // blinking, rain, bouncing…
      redraw();
    } else if (mode == Mode::CharPick && since > face::frameMs(charSel)) {
      lastDrawMs = millis();
      redraw();
    } else if ((mode == Mode::Status && since > 1000) || since > 30000) {
      lastDrawMs = millis();  // live status page; battery/health in the bar elsewhere
      if (mode != Mode::Listening && mode != Mode::Notice && mode != Mode::Image) redraw();
    }
  }

  noteButtonActivity();
  handleIdlePower();
  delay(1);
}
