#include "notes.h"

#include <FS.h>
#include <LittleFS.h>
#include <M5Unified.h>
#include <SD.h>
#include <SPI.h>

#include <algorithm>

#include "board.h"

namespace notes {

static constexpr const char* kDir = "/claw-notes";
static constexpr size_t kHeader = 44;
static constexpr size_t kKeepFree = 128 * 1024;  // flash: room left for the paper's answer cards
static fs::FS* store = nullptr;
static bool onCard = false;
static int cachedCount = 0;
static uint64_t cachedSamples = 0;
static uint64_t cachedFree = 0;  // room() reads this: asking the card would need its bus
static File reader;

static uint64_t freeBytes() {
  if (!store) return 0;
  if (onCard) return SD.totalBytes() - SD.usedBytes();
  return LittleFS.totalBytes() - LittleFS.usedBytes();
}

static String pathOf(const String& id) { return String(kDir) + "/" + id + ".wav"; }

static void put32(uint8_t* p, uint32_t v) {
  p[0] = v, p[1] = v >> 8, p[2] = v >> 16, p[3] = v >> 24;
}

// A plain PCM WAV header, so a card read on a computer plays.
static void wavHeader(uint8_t* h, size_t samples, uint32_t rate) {
  const uint32_t bytes = samples * 2;
  memcpy(h, "RIFF", 4);
  put32(h + 4, 36 + bytes);
  memcpy(h + 8, "WAVEfmt ", 8);
  put32(h + 16, 16);
  h[20] = 1, h[21] = 0;  // PCM
  h[22] = 1, h[23] = 0;  // mono
  put32(h + 24, rate);
  put32(h + 28, rate * 2);
  h[32] = 2, h[33] = 0;   // block align
  h[34] = 16, h[35] = 0;  // bits
  memcpy(h + 36, "data", 4);
  put32(h + 40, bytes);
}

// The id and size of each kept note; broken leftovers (a write cut short)
// are removed.
static std::vector<std::pair<String, size_t>> scan() {
  std::vector<std::pair<String, size_t>> out;
  std::vector<String> broken;
  File dir = store ? store->open(kDir) : File();
  if (!dir || !dir.isDirectory()) return out;
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    String name = f.name();
    int slash = name.lastIndexOf('/');
    if (slash >= 0) name = name.substring(slash + 1);
    size_t size = f.size();
    f.close();
    if (!name.endsWith(".wav")) continue;
    name.remove(name.length() - 4);
    if (size <= kHeader) broken.push_back(name);
    else out.push_back({name, size});
  }
  dir.close();
  for (auto& id : broken) store->remove(pathOf(id));
  typedef std::pair<String, size_t> Entry;
  std::sort(out.begin(), out.end(), [](const Entry& a, const Entry& b) { return a.first < b.first; });
  return out;
}

static void recount() {
  cachedCount = 0;
  cachedSamples = 0;
  for (auto& f : scan()) {
    ++cachedCount;
    cachedSamples += (f.second - kHeader) / 2;
  }
  cachedFree = freeBytes();
}

bool begin() {
  if (!board::kOfflineNotes) return false;
  if (board::kHasSdCard) {
    int cs = M5.getPin(m5::pin_name_t::sd_spi_ss);
    // The panel's driver already set the shared bus up (SPI); the card only
    // needs its own chip select. No card: SD.begin fails and flash is used.
    if (cs >= 0 && cs < 255 && SD.begin(cs, SPI, 20000000, "/sd", 2)) {
      store = &SD;
      onCard = true;
    }
  }
  if (!store && LittleFS.begin(true, "/littlefs", 10, "spiffs")) store = &LittleFS;
  if (!store) return false;
  if (!store->exists(kDir)) store->mkdir(kDir);
  recount();
  Serial.printf("[claw] offline notes on %s: %d waiting, %llu KB free\n", where(), cachedCount,
                (unsigned long long)(cachedFree / 1024));
  return true;
}

const char* where() { return !store ? "" : onCard ? "TF card" : "flash"; }

size_t room(uint32_t rate) {
  uint64_t free = cachedFree;
  if (free < kKeepFree + kHeader + rate * 2) return 0;  // under a second
  return (size_t)min<uint64_t>((free - kKeepFree - kHeader) / 2, 0x7FFFFFFF);
}

bool save(const int16_t* pcm, size_t samples, uint32_t rate, time_t when) {
  if (!store || !samples || samples > room(rate)) return false;
  char path[64];
  snprintf(path, sizeof(path), "%s/%010lu-%08lx.wav", kDir, (unsigned long)(when > 0 ? when : 0),
           (unsigned long)esp_random());
  File f = store->open(path, FILE_WRITE);
  if (!f) return false;
  uint8_t h[kHeader];
  wavHeader(h, samples, rate);
  bool ok = f.write(h, kHeader) == kHeader;
  const uint8_t* bytes = (const uint8_t*)pcm;
  const size_t total = samples * 2;
  for (size_t off = 0; ok && off < total; off += 16384) {  // in pieces: flash writes stall the cache
    size_t n = min(total - off, (size_t)16384);
    ok = f.write(bytes + off, n) == n;
  }
  f.close();
  if (!ok) {
    store->remove(path);
    cachedFree = freeBytes();
    return false;
  }
  ++cachedCount;
  cachedSamples += samples;
  cachedFree = freeBytes();
  return true;
}

std::vector<Note> list(size_t max) {
  std::vector<Note> out;
  for (auto& f : scan()) {
    if (out.size() >= max) break;
    Note n;
    n.id = f.first;
    n.when = (time_t)strtoul(f.first.c_str(), nullptr, 10);
    n.samples = (f.second - kHeader) / 2;
    out.push_back(n);
  }
  return out;
}

int count() { return cachedCount; }

uint32_t seconds() { return cachedSamples / 16000; }

bool openRead(const Note& note, uint32_t* rate) {
  closeRead();
  if (!store) return false;
  reader = store->open(pathOf(note.id), FILE_READ);
  uint8_t h[kHeader];
  if (!reader || reader.read(h, kHeader) != kHeader || memcmp(h, "RIFF", 4) != 0) {
    closeRead();
    return false;
  }
  *rate = h[24] | (h[25] << 8) | (h[26] << 16) | ((uint32_t)h[27] << 24);
  if (*rate < 8000 || *rate > 48000) *rate = 16000;
  return true;
}

size_t read(uint8_t* buf, size_t max) { return reader ? reader.read(buf, max & ~(size_t)1) : 0; }

void closeRead() {
  if (reader) reader.close();
}

void remove(const String& id) {
  if (!store) return;
  store->remove(pathOf(id));
  recount();
}

void quarantine(const String& id) {
  if (!store) return;
  closeRead();
  store->rename(pathOf(id), String(kDir) + "/" + id + ".bad");
  recount();
}

}  // namespace notes
