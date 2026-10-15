#include "audio.h"

#include <M5Unified.h>

namespace audio {

// ── microphone: a small ring of blocks; M5Unified fills up to two at once ──
static constexpr size_t kBlock = 512;  // 32 ms at 16 kHz
static constexpr int kRing = 6;
static int16_t* micBuf = nullptr;      // kRing * kBlock samples
static int fifo[kRing];                // submitted block indices, oldest first
static int fifoLen = 0;
static int nextBlock = 0;
static bool micOn = false;
static int lastLevel = 0;

// ── speaker: one append-only PSRAM buffer per stream ──
static constexpr size_t kPlayCap = 1600 * 1024;  // samples (~66 s at 24 kHz)
static int16_t* playBuf = nullptr;
static size_t writePos = 0, playPos = 0;  // in samples
static uint32_t playRate = 24000;
static bool streamOpen = false, streamEnded = false, started = false;
static int carry = -1;                    // odd byte waiting for its pair

void begin(uint8_t volume) {
  micBuf = (int16_t*)heap_caps_malloc(kRing * kBlock * sizeof(int16_t), MALLOC_CAP_8BIT);
  playBuf = (int16_t*)heap_caps_malloc(kPlayCap * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  M5.Speaker.setVolume(volume);
  M5.Speaker.end();
  M5.Mic.end();
}

void setVolume(uint8_t volume) { M5.Speaker.setVolume(volume); }

static void submitNext() {
  int16_t* dst = micBuf + nextBlock * kBlock;
  if (M5.Mic.record(dst, kBlock, kMicRate)) {
    fifo[fifoLen++] = nextBlock;
    nextBlock = (nextBlock + 1) % kRing;
  }
}

static void drainDone(size_t inflight, void (*sink)(const int16_t*, size_t)) {
  while (fifoLen > (int)inflight) {
    const int16_t* blk = micBuf + fifo[0] * kBlock;
    int peak = 0;
    for (size_t i = 0; i < kBlock; ++i) peak = max(peak, abs((int)blk[i]));
    lastLevel = min(100, peak * 100 / 12000);
    sink(blk, kBlock);
    for (int i = 1; i < fifoLen; ++i) fifo[i - 1] = fifo[i];
    --fifoLen;
  }
}

void micStart() {
  playStop();
  M5.Speaker.end();
  M5.Mic.begin();
  fifoLen = 0;
  nextBlock = 0;
  lastLevel = 0;
  micOn = true;
  submitNext();
  submitNext();
}

int micPoll(void (*sink)(const int16_t*, size_t)) {
  if (!micOn) return 0;
  drainDone(M5.Mic.isRecording(), sink);
  while (fifoLen < 2) {
    int before = fifoLen;
    submitNext();
    if (fifoLen == before) break;
  }
  return lastLevel;
}

void micStop(void (*sink)(const int16_t*, size_t)) {
  if (!micOn) return;
  uint32_t t0 = millis();
  while (M5.Mic.isRecording() && millis() - t0 < 200) delay(1);
  drainDone(0, sink);
  M5.Mic.end();
  micOn = false;
}

bool micActive() { return micOn; }

void playBegin(uint32_t rate) {
  playStop();
  if (micOn) micStop([](const int16_t*, size_t) {});
  M5.Mic.end();
  if (!M5.Speaker.isEnabled()) M5.Speaker.begin();
  playRate = rate ? rate : 24000;
  writePos = playPos = 0;
  carry = -1;
  streamOpen = true;
  streamEnded = false;
  started = false;
}

void playFeed(const uint8_t* data, size_t len) {
  if (!streamOpen || !playBuf) return;
  size_t i = 0;
  if (carry >= 0 && len) {
    if (writePos < kPlayCap) playBuf[writePos++] = (int16_t)(carry | (data[0] << 8));
    carry = -1;
    i = 1;
  }
  size_t pairs = (len - i) / 2;
  size_t room = kPlayCap - writePos;
  size_t n = min(pairs, room);
  memcpy(playBuf + writePos, data + i, n * 2);  // little-endian on the ESP32 too
  writePos += n;
  if ((len - i) % 2 && n == pairs) carry = data[len - 1];
}

void playEnd() { streamEnded = true; }

void playStop() {
  if (M5.Speaker.isEnabled()) M5.Speaker.stop();
  streamOpen = false;
  streamEnded = false;
  started = false;
  writePos = playPos = 0;
}

void playPoll() {
  if (!streamOpen) return;
  size_t avail = writePos - playPos;
  size_t queued = M5.Speaker.isPlaying(0);
  if (!started) {
    // Buffer a little first so a slow network doesn't stutter the start.
    if (avail < playRate * 3 / 10 && !streamEnded) return;
    started = true;
  }
  if (queued < 2 && avail > 0 && (avail >= playRate / 10 || streamEnded)) {
    size_t n = min(avail, (size_t)(playRate / 2));
    M5.Speaker.playRaw(playBuf + playPos, n, playRate, false, 1, 0, false);
    playPos += n;
  }
  if (streamEnded && playPos == writePos && queued == 0) {
    streamOpen = false;  // stream finished and fully played
  }
}

bool playing() { return streamOpen; }

}  // namespace audio
