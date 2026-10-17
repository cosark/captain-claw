#include "ota.h"

#include <HTTPClient.h>
#include <Update.h>
#include <WiFiClientSecure.h>
#include <esp_ota_ops.h>
#include <mbedtls/base64.h>
#include <mbedtls/pk.h>
#include <mbedtls/sha256.h>

#include <memory>

#include "ota_pubkey.h"

// The Arduino core marks a new app valid at boot unless this says the app
// will decide; ours decides once it has reached Flight Deck (ota::markGood).
extern "C" bool verifyRollbackLater() { return true; }

namespace ota {

int compareVersions(const String& a, const String& b) {
  int i = 0, j = 0;
  while (i < (int)a.length() || j < (int)b.length()) {
    long x = 0, y = 0;
    while (i < (int)a.length() && isdigit(a[i])) x = x * 10 + (a[i++] - '0');
    while (j < (int)b.length() && isdigit(b[j])) y = y * 10 + (b[j++] - '0');
    if (x != y) return x < y ? -1 : 1;
    while (i < (int)a.length() && !isdigit(a[i])) ++i;  // the dots
    while (j < (int)b.length() && !isdigit(b[j])) ++j;
  }
  return 0;
}

bool onProbation() {
  esp_ota_img_states_t state;
  const esp_partition_t* running = esp_ota_get_running_partition();
  return running && esp_ota_get_state_partition(running, &state) == ESP_OK &&
         state == ESP_OTA_IMG_PENDING_VERIFY;
}

void markGood() { esp_ota_mark_app_valid_cancel_rollback(); }

void rollBack() {
  esp_ota_mark_app_invalid_rollback_and_reboot();
  ESP.restart();  // no previous version to go back to: just restart
  for (;;) {
  }
}

static String hex(const uint8_t* d, size_t n) {
  static const char* h = "0123456789abcdef";
  String s;
  s.reserve(n * 2);
  for (size_t i = 0; i < n; ++i) {
    s += h[d[i] >> 4];
    s += h[d[i] & 15];
  }
  return s;
}

// ECDSA P-256 over the firmware's SHA-256, against the key built into us.
static bool signatureOk(const uint8_t hash[32], const String& sigB64) {
  uint8_t sig[160];
  size_t sigLen = 0;
  if (mbedtls_base64_decode(sig, sizeof(sig), &sigLen, (const uint8_t*)sigB64.c_str(), sigB64.length()) != 0) {
    return false;
  }
  mbedtls_pk_context pk;
  mbedtls_pk_init(&pk);
  bool ok = mbedtls_pk_parse_public_key(&pk, (const uint8_t*)kOtaPublicKey, sizeof(kOtaPublicKey)) == 0 &&
            mbedtls_pk_verify(&pk, MBEDTLS_MD_SHA256, hash, 32, sig, sigLen) == 0;
  mbedtls_pk_free(&pk);
  return ok;
}

String install(const Offer& offer, const String& base, const String& token,
               void (*progress)(size_t done, size_t total)) {
  if (!offer.valid()) return "incomplete update offer";
  String url = base + offer.url;
  std::unique_ptr<WiFiClient> client;
  if (url.startsWith("https://")) {
    auto* tls = new WiFiClientSecure;
    tls->setInsecure();  // the signature, not TLS, is what vouches for the build
    client.reset(tls);
  } else {
    client.reset(new WiFiClient);
  }
  HTTPClient http;
  http.setTimeout(20000);
  if (!http.begin(*client, url)) return "bad update address";
  if (token.length()) http.addHeader("Authorization", "Bearer " + token);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    http.end();
    return "download failed (HTTP " + String(code) + ")";
  }
  int len = http.getSize();
  if (len <= 0 || (size_t)len != offer.size) {
    http.end();
    return "unexpected size";
  }
  if (!Update.begin(len, U_FLASH)) {
    http.end();
    return String("no room: ") + Update.errorString();
  }
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  mbedtls_sha256_starts_ret(&sha, 0);
  std::unique_ptr<uint8_t[]> buf(new uint8_t[4096]);
  WiFiClient* in = http.getStreamPtr();
  size_t done = 0;
  uint32_t lastData = millis();
  String err;
  while (done < (size_t)len) {
    size_t avail = in->available();
    if (!avail) {
      if (millis() - lastData > 20000) {
        err = "download stalled";
        break;
      }
      delay(2);
      continue;
    }
    size_t n = in->readBytes(buf.get(), min(avail, min((size_t)4096, (size_t)len - done)));
    if (!n) continue;
    lastData = millis();
    mbedtls_sha256_update_ret(&sha, buf.get(), n);
    if (Update.write(buf.get(), n) != n) {
      err = String("write failed: ") + Update.errorString();
      break;
    }
    done += n;
    if (progress) progress(done, len);
  }
  http.end();
  uint8_t hash[32];
  mbedtls_sha256_finish_ret(&sha, hash);
  mbedtls_sha256_free(&sha);
  if (err.isEmpty() && hex(hash, 32) != offer.sha256) err = "checksum mismatch";
  if (err.isEmpty() && !signatureOk(hash, offer.sig)) err = "signature not trusted";
  if (!err.isEmpty()) {
    Update.abort();
    return err;
  }
  // Checks the image, then makes the spare slot the one to boot.
  if (!Update.end(true)) return String("install failed: ") + Update.errorString();
  return "";
}

}  // namespace ota
