// Captain Claw voice client for the M5Stack StickS3.
//
//   Front button (A)  hold = talk, release = send · tap = stop speaking / flip picture↔text
//   Side button  (B)  click = next page · hold = agents, volume, boost, settings
//   Hold A while switching on (or no Wi-Fi saved) = setup hotspot
//
// Talks to Flight Deck's /device/ws (captain_claw/flight_deck/device_bridge.py):
// 16 kHz PCM up; reply text, 24 kHz PCM speech and JPEG pictures down.

#include <ArduinoJson.h>
#include <M5Unified.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <esp_sleep.h>

#include "audio.h"
#include "portal.h"
#include "settings.h"
#include "ui.h"

enum class Mode { Idle, Listening, Waiting, Reply, Image, Picker, Notice };

static WebSocketsClient ws;
static WiFiMulti wifiMulti;
static bool wsUp = false;
static Mode mode = Mode::Idle;
static String agentName, agentSlug;
static String heard, statusLine;
static std::vector<String> activity;  // what the agent is doing, oldest first
static int toolSteps = 0;
static uint32_t workStartMs = 0;
static bool workDirty = false;
static uint32_t recStartMs = 0, lastActivityMs = 0, lastDrawMs = 0, pickerOpenedMs = 0;
static bool dimmed = false, screenOff = false;
static std::vector<String> pickNames, pickSlugs;
static int pickSel = 0, pickCur = -1;
static bool pickerLoading = false;

static void addSettingItems();

static constexpr uint32_t kTapMs = 300;      // shorter press = tap, not talk
static constexpr uint32_t kDimMs = 30000;
static constexpr uint32_t kOffMs = 60000;
static constexpr uint8_t kBright = 128;

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

// "working 12s · 3 steps" while the agent runs.
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
  const char* st = !wsUp ? "offline" : audio::playing() ? "speaking"
                 : mode == Mode::Listening ? "listening" : mode == Mode::Waiting ? workLabel() : "ready";
  uint32_t col = !wsUp ? TFT_RED : mode == Mode::Listening ? TFT_RED
               : audio::playing() ? TFT_CYAN : TFT_GREEN;
  ui::setBar(agentName, st, col);
}

static void redraw() {
  setBarState();
  switch (mode) {
    case Mode::Listening: ui::showListening(0, millis() - recStartMs); break;
    case Mode::Waiting: ui::showWorking(heard, activity, statusLine); break;
    case Mode::Reply: ui::showReply(); break;
    case Mode::Image: ui::showStoredImage(); break;
    case Mode::Picker: ui::showPicker(pickNames, pickSel, pickCur); break;
    case Mode::Notice: break;  // stays until the next event
    case Mode::Idle:
      if (ui::hasReply()) {
        ui::showReply();
      } else {
        ui::showMessage(wsUp ? "Hold the front button" : "Connecting…",
                        wsUp ? String("and talk. Release to send.") : settings.server);
      }
      break;
  }
}

static void notice(const char* title, const String& body, uint32_t color) {
  mode = Mode::Notice;
  setBarState();
  ui::showMessage(title, body, color);
}

// ── microphone → server ──────────────────────────────────────────────

static void micSink(const int16_t* samples, size_t count) {
  if (wsUp) ws.sendBIN((const uint8_t*)samples, count * sizeof(int16_t));
}

static void startTalking() {
  if (!wsUp) {
    notice("Offline", "Not connected to Flight Deck yet.", TFT_RED);
    return;
  }
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
  } else {
    mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
  }
  redraw();
}

// ── server → device ──────────────────────────────────────────────────

static void onText(const char* payload, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  String type = doc["type"] | "";

  if (type == "bound") {
    agentName = doc["agent"] | "";
    agentSlug = doc["slug"] | "";
    if (mode == Mode::Picker || mode == Mode::Idle || mode == Mode::Notice) mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
    redraw();
  } else if (type == "agents") {
    pickNames.clear();
    pickSlugs.clear();
    pickCur = -1;
    String current = doc["current"] | "";
    for (JsonObject a : doc["items"].as<JsonArray>()) {
      String slug = a["slug"] | "";
      if (slug == current) pickCur = pickNames.size();
      pickNames.push_back(a["name"] | slug);
      pickSlugs.push_back(slug);
    }
    addSettingItems();
    pickSel = max(0, pickCur);
    pickerLoading = false;
    if (mode == Mode::Picker) redraw();
  } else if (type == "state") {
    String st = doc["state"] | "";
    if (st == "transcribing") {
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
    if (mode == Mode::Waiting || mode == Mode::Idle || st == "ready") redraw();
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
    String text = doc["text"] | "";
    mode = Mode::Reply;
    setBarState();
    ui::setReply(text);
  } else if (type == "audio_start") {
    audio::playBegin(doc["rate"] | 24000);
    setBarState();
  } else if (type == "audio_end") {
    audio::playEnd();
  } else if (type == "error") {
    touch();
    notice("Hmm", doc["text"] | "Something went wrong.", TFT_ORANGE);
  }
}

static void onBinary(const uint8_t* data, size_t len) {
  if (len < 2) return;
  if (data[0] == 0x01) {
    audio::playFeed(data + 1, len - 1);
  } else if (data[0] == 0x02) {
    touch();
    ui::showImage(data + 1, len - 1);
    mode = Mode::Image;
  }
}

static void onWs(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      Serial.println("[claw] flight deck connected");
      wsUp = true;
      redraw();
      break;
    case WStype_DISCONNECTED:
      if (wsUp) {
        Serial.println("[claw] flight deck disconnected");
        wsUp = false;
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

// ── picker ───────────────────────────────────────────────────────────
// The agent list ends with three setting rows: volume, boost, Wi-Fi & settings.

static constexpr uint8_t kVolumeSteps[] = {64, 128, 191, 255};  // 25 / 50 / 75 / 100 %
static constexpr uint8_t kBoostSteps[] = {0, 6, 10};
static int volumeRow = -1, boostRow = -1, setupRow = -1;

static String volumeLabel() { return "Volume " + String((settings.volume * 100 + 127) / 255) + "%"; }
static String boostLabel() {
  return settings.boostDb ? "Boost +" + String(settings.boostDb) + " dB" : String("Boost off");
}

static void addSettingItems() {
  volumeRow = pickNames.size();
  pickNames.push_back(volumeLabel());
  pickSlugs.push_back("");
  boostRow = pickNames.size();
  pickNames.push_back(boostLabel());
  pickSlugs.push_back("");
  setupRow = pickNames.size();
  pickNames.push_back("Wi-Fi & settings…");
  pickSlugs.push_back("");
}

// Next step up (wrapping) from the current value.
template <size_t N>
static uint8_t nextStep(const uint8_t (&steps)[N], uint8_t now) {
  for (uint8_t v : steps) {
    if (v > now) return v;
  }
  return steps[0];
}

static void openPicker() {
  if (!wsUp) {
    runSetupPortal("Settings");
  }
  pickNames.clear();
  pickSlugs.clear();
  pickNames.push_back("Loading agents…");
  pickSlugs.push_back("");
  pickSel = 0;
  pickCur = -1;
  pickerLoading = true;
  pickerOpenedMs = millis();
  mode = Mode::Picker;
  sendType("agents");
  redraw();
}

static void choosePicked() {
  if (pickerLoading) return;
  if (pickSel == setupRow) runSetupPortal("Settings");
  if (pickSel == volumeRow || pickSel == boostRow) {  // change in place, stay in the list
    if (pickSel == volumeRow) {
      settings.volume = nextStep(kVolumeSteps, settings.volume);
      audio::setVolume(settings.volume);
      pickNames[volumeRow] = volumeLabel();
    } else {
      settings.boostDb = nextStep(kBoostSteps, settings.boostDb);
      audio::setBoost(settings.boostDb);
      pickNames[boostRow] = boostLabel();
    }
    settings.save();
    pickerOpenedMs = millis();
    redraw();
    audio::beep();
    return;
  }
  settings.agent = pickSlugs[pickSel];
  settings.save();
  JsonDocument d;
  d["type"] = "select";
  d["agent"] = settings.agent;
  sendJson(d);
  mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
  redraw();
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

static void handleIdlePower() {
  if (mode == Mode::Listening || audio::playing()) {
    lastActivityMs = millis();
    return;
  }
  uint32_t idle = millis() - lastActivityMs;
  if (!dimmed && idle > kDimMs) {
    ui::brightness(24);
    dimmed = true;
  }
  if (!screenOff && idle > kOffMs) {
    ui::brightness(0);
    screenOff = true;
  }
  if (settings.sleepMin && idle > settings.sleepMin * 60000UL && mode != Mode::Waiting &&
      !M5.Power.isCharging()) {
    goToSleep();
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
  Serial.printf("[claw] boot %s: %d network(s), server '%s'\n", deviceId().c_str(), settings.netCount, settings.server.c_str());
  audio::begin(settings.volume, settings.boostDb);
  M5.BtnB.setHoldThresh(800);

  M5.update();
  if (M5.BtnA.isPressed() || !settings.configured()) {
    runSetupPortal(settings.configured() ? "Setup" : "First-time setup");
  }

  ui::setBar("", "wi-fi…", TFT_YELLOW);
  ui::showMessage("Connecting to Wi-Fi", settings.nets[0].ssid, TFT_WHITE);
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
  ui::showMessage("Wi-Fi connected", WiFi.SSID() + "  " + WiFi.localIP().toString(), TFT_GREEN);
  connectServer();
  lastActivityMs = millis();
}

void loop() {
  M5.update();
  ws.loop();
  if (WiFi.status() != WL_CONNECTED) wifiMulti.run(0);

  // Front button: hold to talk, tap for quick actions.
  if (M5.BtnA.wasPressed()) {
    touch();
    if (mode == Mode::Picker) {
      choosePicked();
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

  // Side button: page / back · hold = agents & settings.
  if (M5.BtnB.wasHold()) {
    touch();
    if (mode != Mode::Listening) openPicker();
  } else if (M5.BtnB.wasClicked()) {
    touch();
    if (mode == Mode::Picker) {
      if (!pickerLoading) pickSel = (pickSel + 1) % pickNames.size();
      redraw();
    } else if (mode == Mode::Image) {
      mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
      redraw();
    } else if (mode == Mode::Reply) {
      ui::nextPage();
    } else if (mode == Mode::Notice || mode == Mode::Idle) {
      mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
      redraw();
    }
  }
  if (mode == Mode::Picker && millis() - pickerOpenedMs > 20000) {
    mode = ui::hasReply() ? Mode::Reply : Mode::Idle;
    redraw();
  }

  // Keep the mic stream and the speaker fed.
  if (mode == Mode::Listening) {
    int level = audio::micPoll(micSink);
    if (millis() - lastDrawMs > 80) {
      lastDrawMs = millis();
      setBarState();
      ui::showListening(level, millis() - recStartMs);
    }
    if (millis() - recStartMs > 110000) stopTalking(true);  // the server caps at 120 s
  }
  if (mode == Mode::Waiting) {
    static uint32_t lastSecond = 0;
    uint32_t secs = (millis() - workStartMs) / 1000;
    if ((workDirty && millis() - lastDrawMs > 150) || secs != lastSecond) {
      lastSecond = secs;
      lastDrawMs = millis();
      workDirty = false;
      redraw();
    }
  }
  bool wasPlaying = audio::playing();
  audio::playPoll();
  if (wasPlaying != audio::playing() && (mode == Mode::Reply || mode == Mode::Idle)) {
    setBarState();
    ui::showReply();
  }

  handleIdlePower();
  delay(1);
}
