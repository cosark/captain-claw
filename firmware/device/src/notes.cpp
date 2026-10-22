#include "notes.h"

#include <FS.h>
#include <LittleFS.h>

#include <algorithm>

#include "adpcm.h"
#include "board.h"

namespace notes {

// A note is "<id>.adp": a 16-byte header ("CCA1", rate, samples, 0), then
// two 4-bit ADPCM codes a byte, low nibble first. Older firmware kept 16 kHz
// PCM WAVs ("<id>.wav"); those are read as they are.
static constexpr const char* kDir = "/claw-notes";
static constexpr size_t kHead = 16;
static constexpr size_t kWavHead = 44;
static constexpr size_t kKeepFree = 128 * 1024;  // room left for the paper's answer cards
static bool mounted = false;
static int cachedCount = 0;
static uint32_t cachedMs = 0;
static uint64_t cachedFree = 0;
static File reader;
static bool readerPacked = false;
static size_t readerLeft = 0;  // samples still to come
static adpcm::State readerState;

static uint64_t freeBytes() { return mounted ? LittleFS.totalBytes() - LittleFS.usedBytes() : 0; }

static String pathOf(const String& id, const char* ext) { return String(kDir) + "/" + id + ext; }

// What a recording at `rate` is kept at: speech needs no more than 8 kHz.
static uint32_t keptRate(uint32_t rate) { return rate >= 16000 ? rate / 2 : rate; }

static void put32(uint8_t* p, uint32_t v) {
  p[0] = v, p[1] = v >> 8, p[2] = v >> 16, p[3] = v >> 24;
}

static uint32_t get32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }

struct Entry {
  String id;
  size_t size;
  bool packed;  // .adp (else an old .wav)
};

static Note noteOf(const Entry& e) {
  Note n;
  n.id = e.id;
  n.when = (time_t)strtoul(e.id.c_str(), nullptr, 10);
  if (e.packed) {
    n.rate = keptRate(16000);
    n.samples = (e.size - kHead) * 2;
  } else {
    n.rate = 16000;
    n.samples = (e.size - kWavHead) / 2;
  }
  return n;
}

// Each kept note; broken leftovers (a write cut short) are removed.
static std::vector<Entry> scan() {
  std::vector<Entry> out;
  std::vector<String> broken;
  File dir = mounted ? LittleFS.open(kDir) : File();
  if (!dir || !dir.isDirectory()) return out;
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    String name = f.name();
    int slash = name.lastIndexOf('/');
    if (slash >= 0) name = name.substring(slash + 1);
    size_t size = f.size();
    f.close();
    bool packed = name.endsWith(".adp");
    if (!packed && !name.endsWith(".wav")) continue;
    if (size <= (packed ? kHead : kWavHead)) broken.push_back(name);
    else out.push_back({name.substring(0, name.length() - 4), size, packed});
  }
  dir.close();
  for (auto& name : broken) LittleFS.remove(String(kDir) + "/" + name);
  std::sort(out.begin(), out.end(), [](const Entry& a, const Entry& b) { return a.id < b.id; });
  return out;
}

static void recount() {
  cachedCount = 0;
  cachedMs = 0;
  for (auto& e : scan()) {
    Note n = noteOf(e);
    ++cachedCount;
    cachedMs += (uint64_t)n.samples * 1000 / n.rate;
  }
  cachedFree = freeBytes();
}

bool begin() {
  if (!board::kOfflineNotes) return false;
  mounted = LittleFS.begin(true, "/littlefs", 10, "spiffs");
  if (!mounted) return false;
  if (!LittleFS.exists(kDir)) LittleFS.mkdir(kDir);
  recount();
  Serial.printf("[claw] offline notes: %d waiting, %llu KB free (~%lu min)\n", cachedCount,
                (unsigned long long)(cachedFree / 1024), (unsigned long)(room(16000) / 16000 / 60));
  return true;
}

bool ready() { return mounted; }

size_t room(uint32_t rate) {
  const uint64_t perSecond = keptRate(rate) / 2;  // bytes: two codes a byte
  if (!rate || cachedFree < kKeepFree + kHead + perSecond) return 0;  // under a second
  return (size_t)min<uint64_t>((cachedFree - kKeepFree - kHead) * rate / perSecond, 0x7FFFFFFF);
}

bool save(const int16_t* pcm, size_t samples, uint32_t rate, time_t when) {
  if (!mounted || !samples || samples > room(rate)) return false;
  const bool halve = keptRate(rate) != rate;
  const size_t kept = halve ? samples / 2 : samples;
  char path[64];
  snprintf(path, sizeof(path), "%s/%010lu-%08lx.adp", kDir, (unsigned long)(when > 0 ? when : 0),
           (unsigned long)esp_random());
  File f = LittleFS.open(path, FILE_WRITE);
  if (!f) return false;
  uint8_t h[kHead] = {'C', 'C', 'A', '1'};
  put32(h + 4, keptRate(rate));
  put32(h + 8, kept);
  bool ok = f.write(h, kHead) == kHead;
  static uint8_t piece[4096];  // a flash sector a write: flash writes stall the cache
  size_t fill = 0;
  adpcm::State st;
  auto at = [&](size_t i) { return halve ? adpcm::halved(pcm, samples, i) : pcm[i]; };
  for (size_t i = 0; ok && i < kept; i += 2) {
    uint8_t b = adpcm::encode(st, at(i));
    if (i + 1 < kept) b |= adpcm::encode(st, at(i + 1)) << 4;
    piece[fill++] = b;
    if (fill == sizeof(piece)) {
      ok = f.write(piece, fill) == fill;
      fill = 0;
    }
  }
  if (ok && fill) ok = f.write(piece, fill) == fill;
  f.close();
  if (!ok) {
    LittleFS.remove(path);
    cachedFree = freeBytes();
    return false;
  }
  ++cachedCount;
  cachedMs += (uint64_t)kept * 1000 / keptRate(rate);
  cachedFree = freeBytes();
  return true;
}

std::vector<Note> list(size_t max) {
  std::vector<Note> out;
  for (auto& e : scan()) {
    if (out.size() >= max) break;
    out.push_back(noteOf(e));
  }
  return out;
}

int count() { return cachedCount; }

uint32_t seconds() { return cachedMs / 1000; }

bool openRead(const Note& note, uint32_t* rate) {
  closeRead();
  if (!mounted) return false;
  readerPacked = LittleFS.exists(pathOf(note.id, ".adp"));
  reader = LittleFS.open(pathOf(note.id, readerPacked ? ".adp" : ".wav"), FILE_READ);
  uint8_t h[kWavHead];
  const size_t head = readerPacked ? kHead : kWavHead;
  if (!reader || reader.read(h, head) != head || memcmp(h, readerPacked ? "CCA1" : "RIFF", 4) != 0) {
    closeRead();
    return false;
  }
  *rate = get32(h + (readerPacked ? 4 : 24));
  if (*rate < 8000 || *rate > 48000) *rate = readerPacked ? 8000 : 16000;
  readerLeft = readerPacked ? get32(h + 8) : 0;
  readerState = adpcm::State();
  return true;
}

size_t read(uint8_t* buf, size_t max) {
  if (!reader) return 0;
  if (!readerPacked) return reader.read(buf, max & ~(size_t)1);
  // Decoded as it's read: whole bytes (two samples) but for a note's odd last one.
  const size_t want = min(max / 2 & ~(size_t)1, readerLeft);
  uint8_t packed[512];
  size_t done = 0;
  while (done < want) {
    size_t got = reader.read(packed, min((want - done + 1) / 2, sizeof(packed)));
    if (!got) break;
    for (size_t i = 0; i < got && done < want; ++i) {
      for (int half = 0; half < 2 && done < want; ++half, ++done) {
        int16_t s = adpcm::decode(readerState, half ? packed[i] >> 4 : packed[i] & 15);
        buf[2 * done] = (uint8_t)s;
        buf[2 * done + 1] = (uint8_t)(s >> 8);
      }
    }
  }
  readerLeft -= done;
  return done * 2;
}

void closeRead() {
  if (reader) reader.close();
}

void remove(const String& id) {
  if (!mounted) return;
  if (!LittleFS.remove(pathOf(id, ".adp"))) LittleFS.remove(pathOf(id, ".wav"));
  recount();
}

void quarantine(const String& id) {
  if (!mounted) return;
  closeRead();
  if (!LittleFS.rename(pathOf(id, ".adp"), pathOf(id, ".bad"))) LittleFS.rename(pathOf(id, ".wav"), pathOf(id, ".bad"));
  recount();
}

}  // namespace notes
