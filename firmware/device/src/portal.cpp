#include "portal.h"

#include <DNSServer.h>
#include <M5Unified.h>
#include <WebServer.h>
#include <WiFi.h>

#include "board.h"
#include "face.h"
#include "settings.h"
#include "ui.h"

static WebServer server(80);
static DNSServer dns;
static String scanOptions;

static String esc(const String& s) {
  String o;
  o.reserve(s.length() + 8);
  for (char c : s) {
    switch (c) {
      case '&': o += "&amp;"; break;
      case '<': o += "&lt;"; break;
      case '>': o += "&gt;"; break;
      case '"': o += "&quot;"; break;
      default: o += c;
    }
  }
  return o;
}

static void scanNetworks() {
  int n = WiFi.scanNetworks(false, false);
  scanOptions = "";
  for (int i = 0; i < n; ++i) {
    String ssid = WiFi.SSID(i);
    if (ssid.isEmpty() || scanOptions.indexOf("value=\"" + esc(ssid) + "\"") >= 0) continue;
    scanOptions += "<option value=\"" + esc(ssid) + "\">" + esc(ssid) + " (" + String(WiFi.RSSI(i)) +
                   " dBm)</option>";
  }
  WiFi.scanDelete();
}

static const char kStyle[] PROGMEM = R"(
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:480px;margin:0 auto;padding:16px;
background:#111;color:#eee}h1{font-size:20px}h2{font-size:15px;margin-top:22px;color:#9cf}
label{display:block;margin:10px 0 4px;font-size:13px;color:#aaa}input,select{width:100%;box-sizing:border-box;
padding:10px;border-radius:8px;border:1px solid #444;background:#222;color:#eee;font-size:15px}
button{margin-top:18px;width:100%;padding:12px;border:0;border-radius:8px;background:#3a7bd5;color:#fff;
font-size:16px}.net{display:flex;justify-content:space-between;align-items:center;padding:6px 0;
border-bottom:1px solid #333}.net label{margin:0;color:#eee;font-size:14px}.net input{width:auto}
small{color:#888}</style>)";

static void handleRoot() {
  String h;
  h.reserve(6000);
  h += F("<!doctype html><html><head><meta charset=utf-8><title>Claw ");
  h += board::kModel;
  h += F(" setup</title>");
  h += FPSTR(kStyle);
  h += F("</head><body><h1>Captain Claw &middot; ");
  h += board::kModel;
  h += F("</h1><form method=post action=/save>");

  h += F("<h2>Wi-Fi networks</h2>");
  if (settings.netCount == 0) h += F("<small>None saved yet.</small>");
  for (int i = 0; i < settings.netCount; ++i) {
    h += "<div class=net><label>" + esc(settings.nets[i].ssid) +
         "</label><label><input type=checkbox name=del" + String(i) + "> remove</label></div>";
  }
  h += F("<label>Add a network</label><select name=pick><option value=''>&mdash; pick a scanned network &mdash;</option>");
  h += scanOptions;
  h += F("</select><label>&hellip;or type its name</label><input name=ssid autocapitalize=none>"
         "<label>Password</label><input name=pass type=password autocomplete=off>");

  h += F("<h2>Flight Deck</h2><label>Address</label><input name=server placeholder='ws://192.168.1.10:25080' value=\"");
  h += esc(settings.server);
  h += F("\" autocapitalize=none><small>ws://host:port on your network, or wss://your.domain</small>"
         "<label>Access token</label><input name=token autocomplete=off autocapitalize=none placeholder='");
  h += settings.token.length() ? F("saved &mdash; leave blank to keep") : F("cc_pat_&hellip;");
  h += F("'><small>Flight Deck &rarr; Connections &rarr; Agent access for Claude (MCP) &rarr; Generate token</small>"
         "<label>Agent (optional)</label><input name=agent autocapitalize=none value=\"");
  h += esc(settings.agent);
  h += F("\" placeholder='first running agent'>");

  h += F("<h2>Voice</h2><label>Spoken reply language</label><input name=lang autocapitalize=none value=\"");
  h += esc(settings.lang);
  h += F("\" placeholder='server default, e.g. en or hr'><label>Read replies aloud</label><select name=speak>");
  h += settings.speak ? F("<option value=1 selected>Yes</option><option value=0>No</option>")
                      : F("<option value=1>Yes</option><option value=0 selected>No</option>");
  h += F("</select><label>Volume (%)</label><input name=vol type=number min=0 max=100 value=");
  h += String((settings.volume * 100 + 127) / 255);
  h += '>';
  if (board::kLoudPeakRestarts) h += F("<small>Above 75% on battery the stick may restart on loud peaks.</small>");
  h += F("<label>Speech boost</label><select name=boost>");
  for (int db : {0, 6, 10}) {
    h += "<option value=" + String(db) + (settings.boostDb == db ? " selected>" : ">") +
         (db ? "+" + String(db) + " dB" : String("Off")) + "</option>";
  }
  h += F("</select");
  h += F("><label>Voice</label><select name=voice>");
  h += settings.femaleVoice ? F("<option value=male>Male</option><option value=female selected>Female</option>")
                            : F("<option value=male selected>Male</option><option value=female>Female</option>");
  h += F("</select><label>Before speaking</label><select name=cue>");
  struct { const char* v; const char* label; bool on; } cues[] = {
      {"both", "Beep + flash", settings.cueBeep && settings.cueFlash},
      {"beep", "Beep only", settings.cueBeep && !settings.cueFlash},
      {"flash", "Flash only", !settings.cueBeep && settings.cueFlash},
      {"none", "Nothing", !settings.cueBeep && !settings.cueFlash}};
  for (auto& c : cues) {
    h += String("<option value=") + c.v + (c.on ? " selected>" : ">") + c.label + "</option>";
  }
  if (board::kHasMotor) {
    h += F("</select><label>Buzz before speaking</label><select name=buzz>");
    h += settings.cueBuzz ? F("<option value=1 selected>Yes</option><option value=0>No</option>")
                          : F("<option value=1>Yes</option><option value=0 selected>No</option>");
  }
  h += F("</select><label>Cue comes</label><select name=lead>");
  h += settings.cueLeadS == 2 ? F("<option value=1>1 s before speech</option><option value=2 selected>2 s before speech</option>")
                              : F("<option value=1 selected>1 s before speech</option><option value=2>2 s before speech</option>");
  h += F("</select><h2>Screen</h2><label>Show</label><select name=display>");
  h += settings.iconMode ? F("<option value=text>Text (stages, activity)</option><option value=icons selected>Icons (animated character)</option>")
                         : F("<option value=text selected>Text (stages, activity)</option><option value=icons>Icons (animated character)</option>");
  h += F("</select><label>Text size</label><select name=tsize>");
  {
    const char* sizes[] = {"Small", "Medium", "Large"};
    for (int k = 0; k < 3; ++k) {
      h += "<option value=" + String(k) + (settings.textSize == k ? " selected>" : ">") + sizes[k] + "</option>";
    }
  }
  h += F("</select><label>Character</label><select name=char>");
  for (int k = 0; k < face::KindCount; ++k) {
    h += "<option value=" + String(k) + (settings.character == k ? " selected>" : ">") + face::name(k) + "</option>";
  }
  h += F("</select>");
  if (board::kRound) {  // the round screen shows a clock
    h += F("<label>Time zone</label><input name=tz autocapitalize=none value=\"");
    h += esc(settings.tz);
    h += F("\"><small>POSIX TZ for the clock, e.g. CET-1CEST,M3.5.0,M10.5.0/3 (Central Europe),"
           " GMT0BST,M3.5.0/1,M10.5.0 (UK), EST5EDT,M3.2.0,M11.1.0 (US East)</small>");
  }
  h += F("<h2>Power</h2><label>Screen off when idle</label><select name=scroff>");
  for (int s : {5, 10, 15}) {
    h += "<option value=" + String(s) + (settings.screenOffS == s ? " selected>" : ">") + String(s) + " s</option>";
  }
  h += F("</select><label>Power off when idle</label><select name=pwroff>");
  for (int s : {15, 30, 60}) {
    h += "<option value=" + String(s) + (settings.powerOffS == s ? " selected>" : ">") + String(s) + " s</option>";
  }
  h += F("</select><label>Software updates</label><select name=autoupd>");
  h += settings.autoUpdate ? F("<option value=0>Ask me (menu → Software update)</option><option value=1 selected>Install by themselves (idle, on USB)</option>")
                           : F("<option value=0 selected>Ask me (menu → Software update)</option><option value=1>Install by themselves (idle, on USB)</option>");
  h += F("</select><small>Power off = deep sleep; the ");
  h += board::kTalkButton;
  h += F(" wakes it (a few seconds to reconnect)."
         " Never while it's listening, speaking or waiting for the agent.</small>"
         "<button type=submit>Save &amp; restart</button></form><p><small>Device ");
  h += deviceId();
  h += F("</small></p></body></html>");
  server.send(200, "text/html; charset=utf-8", h);
}

static void handleSave() {
  for (int i = settings.netCount - 1; i >= 0; --i) {
    if (server.hasArg("del" + String(i))) settings.removeNetwork(i);
  }
  String ssid = server.arg("ssid");
  ssid.trim();
  if (ssid.isEmpty()) ssid = server.arg("pick");
  if (ssid.length()) settings.addNetwork(ssid, server.arg("pass"));

  String srv = server.arg("server");
  srv.trim();
  while (srv.endsWith("/")) srv.remove(srv.length() - 1);
  if (srv.length() && !srv.startsWith("ws://") && !srv.startsWith("wss://")) {
    srv = (srv.startsWith("https://") ? "wss://" + srv.substring(8)
           : srv.startsWith("http://") ? "ws://" + srv.substring(7)
                                       : "ws://" + srv);
  }
  settings.server = srv;
  String tok = server.arg("token");
  tok.trim();
  if (tok.length()) settings.token = tok;
  settings.agent = server.arg("agent");
  settings.agent.trim();
  settings.lang = server.arg("lang");
  settings.lang.trim();
  settings.speak = server.arg("speak") != "0";
  settings.volume = constrain(server.arg("vol").toInt(), 0, 100) * 255 / 100;
  settings.boostDb = constrain(server.arg("boost").toInt(), 0, 10);
  int so = server.arg("scroff").toInt(), po = server.arg("pwroff").toInt();
  settings.screenOffS = (so == 5 || so == 10) ? so : 15;
  settings.powerOffS = (po == 15 || po == 30) ? po : 60;
  settings.autoUpdate = server.arg("autoupd") == "1";
  settings.femaleVoice = server.arg("voice") == "female";
  String cue = server.arg("cue");
  settings.cueBeep = cue == "both" || cue == "beep";
  settings.cueFlash = cue == "both" || cue == "flash";
  settings.cueLeadS = server.arg("lead") == "2" ? 2 : 1;
  if (board::kHasMotor) settings.cueBuzz = server.arg("buzz") != "0";
  if (board::kRound) {
    String tz = server.arg("tz");
    tz.trim();
    settings.tz = tz.length() ? tz : String(kDefaultTz);
  }
  settings.iconMode = server.arg("display") == "icons";
  settings.character = constrain(server.arg("char").toInt(), 0, face::KindCount - 1);
  settings.textSize = constrain(server.arg("tsize").toInt(), 0, 2);
  settings.save();

  server.send(200, "text/html; charset=utf-8",
              String(F("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                       "<body style='font-family:system-ui;background:#111;color:#eee;padding:24px'>"
                       "<h2>Saved.</h2><p>The ")) +
                  board::kNoun + F(" restarts and connects now. You can close this page.</p>"));
  delay(800);
  ESP.restart();
}

static void handleCaptive() {
  server.sendHeader("Location", "http://192.168.4.1/", true);
  server.send(302, "text/plain", "");
}

void runSetupPortal(const char* reason) {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_AP_STA);
  scanNetworks();
  String id = deviceId();
  String ap = String("Captain-") + board::kModel + "-" + id.substring(id.length() - 4);
  WiFi.softAP(ap.c_str());
  delay(200);
  dns.start(53, "*", WiFi.softAPIP());

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.on("/rescan", HTTP_GET, [] {
    scanNetworks();
    handleCaptive();
  });
  server.onNotFound(handleCaptive);  // phones' captive-portal probes land on the form
  server.begin();

  Serial.printf("[claw] setup portal: %s — join %s, open http://192.168.4.1\n", reason, ap.c_str());
  ui::showSetup(reason, ap.c_str(), "192.168.4.1");
  uint32_t lastLog = 0;
  for (;;) {
    if (millis() - lastLog > 10000) {  // for a serial monitor attached later
      lastLog = millis();
      Serial.printf("[claw] setup portal open: join %s, open http://192.168.4.1 (%d client(s))\n",
                    ap.c_str(), WiFi.softAPgetStationNum());
    }
    dns.processNextRequest();
    server.handleClient();
    M5.update();
    delay(2);
  }
}
