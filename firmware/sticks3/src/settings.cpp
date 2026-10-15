#include "settings.h"

#include <Preferences.h>
#include <WiFi.h>

Settings settings;

static const char* kNs = "claw";

void Settings::load() {
  Preferences p;
  p.begin(kNs, true);
  netCount = min<int>(p.getInt("n", 0), kMaxNetworks);
  for (int i = 0; i < netCount; ++i) {
    nets[i].ssid = p.getString(("s" + String(i)).c_str(), "");
    nets[i].pass = p.getString(("p" + String(i)).c_str(), "");
  }
  server = p.getString("server", "");
  token = p.getString("token", "");
  agent = p.getString("agent", "");
  lang = p.getString("lang", "");
  speak = p.getBool("speak", true);
  volume = p.getUChar("vol", 191);
  boostDb = p.getUChar("boost", 6);
  sleepMin = p.getUShort("sleep", 15);
  femaleVoice = p.getBool("female", false);
  iconMode = p.getBool("icons", false);
  character = p.getUChar("char", 0);
  cueBeep = p.getBool("cuebeep", true);
  cueFlash = p.getBool("cueflash", true);
  cueLeadS = constrain(p.getUChar("cuelead", 1), 1, 2);
  p.end();
}

void Settings::save() const {
  Preferences p;
  p.begin(kNs, false);
  p.putInt("n", netCount);
  for (int i = 0; i < kMaxNetworks; ++i) {
    String sk = "s" + String(i), pk = "p" + String(i);
    if (i < netCount) {
      p.putString(sk.c_str(), nets[i].ssid);
      p.putString(pk.c_str(), nets[i].pass);
    } else {
      p.remove(sk.c_str());
      p.remove(pk.c_str());
    }
  }
  p.putString("server", server);
  p.putString("token", token);
  p.putString("agent", agent);
  p.putString("lang", lang);
  p.putBool("speak", speak);
  p.putUChar("vol", volume);
  p.putUChar("boost", boostDb);
  p.putUShort("sleep", sleepMin);
  p.putBool("female", femaleVoice);
  p.putBool("icons", iconMode);
  p.putUChar("char", character);
  p.putBool("cuebeep", cueBeep);
  p.putBool("cueflash", cueFlash);
  p.putUChar("cuelead", cueLeadS);
  p.end();
}

bool Settings::addNetwork(const String& ssid, const String& pass) {
  if (ssid.isEmpty()) return false;
  for (int i = 0; i < netCount; ++i) {
    if (nets[i].ssid == ssid) {
      if (pass.length()) nets[i].pass = pass;  // blank keeps the saved password
      WifiNet hit = nets[i];
      for (int j = i; j > 0; --j) nets[j] = nets[j - 1];
      nets[0] = hit;
      return true;
    }
  }
  if (netCount >= kMaxNetworks) netCount = kMaxNetworks - 1;  // drop the oldest
  for (int j = netCount; j > 0; --j) nets[j] = nets[j - 1];
  nets[0] = {ssid, pass};
  ++netCount;
  return true;
}

void Settings::removeNetwork(int index) {
  if (index < 0 || index >= netCount) return;
  for (int j = index; j < netCount - 1; ++j) nets[j] = nets[j + 1];
  --netCount;
  nets[netCount] = {};
}

String deviceId() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char buf[16];
  snprintf(buf, sizeof(buf), "stick-%02x%02x%02x", mac[3], mac[4], mac[5]);
  return String(buf);
}
