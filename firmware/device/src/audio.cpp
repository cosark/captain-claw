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

// ── speaker: one append-only PSRAM buffer holds the last answer's clip ──
static constexpr size_t kPlayCap = 1600 * 1024;  // samples (~66 s at 24 kHz)
static int16_t* playBuf = nullptr;
static size_t writePos = 0, playPos = 0;  // in samples; the clip is [0, writePos)
static uint32_t playRate = 24000;
static bool streamOpen = false, streamEnded = false, started = false;
static bool freshClip = true;             // the next stream starts a new clip
static bool cut = false;                  // a stream of this clip stopped before its end
static uint32_t holdUntilMs = 0;
static uint32_t soundStartMs = 0;         // when the speaker last started from silence…
static size_t soundStartPos = 0;          // …and from which sample
static int carry = -1;                    // odd byte waiting for its pair

// Speech boost: gain, then a soft knee above half scale so loud syllables
// round off instead of clipping (TTS peaks near -3 dBFS, averages ~-20 dBFS).
static float boostGain = 1.0f;

static inline int16_t shape(int16_t s) {
  if (boostGain == 1.0f) return s;
  constexpr float kKnee = 0.5f;
  float x = s * boostGain * (1.0f / 32768.0f);
  float a = fabsf(x);
  if (a > kKnee) a = kKnee + (1.0f - kKnee) * tanhf((a - kKnee) / (1.0f - kKnee));
  return (int16_t)(copysignf(a, x) * 32767.0f);
}

void setBoost(uint8_t db) { boostGain = powf(10.0f, db / 20.0f); }

void begin(uint8_t volume, uint8_t boostDb) {
  micBuf = (int16_t*)heap_caps_malloc(kRing * kBlock * sizeof(int16_t), MALLOC_CAP_8BIT);
  playBuf = (int16_t*)heap_caps_malloc(kPlayCap * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  M5.Speaker.setVolume(volume);
  setBoost(boostDb);
  M5.Speaker.end();
  M5.Mic.end();
}

void setVolume(uint8_t volume) { M5.Speaker.setVolume(volume); }

void beep() {
  if (micOn) return;
  if (!M5.Speaker.isEnabled()) M5.Speaker.begin();
  M5.Speaker.tone(880, 120, 1);
}

void cue() {
  if (micOn) return;
  if (!M5.Speaker.isEnabled()) M5.Speaker.begin();
  M5.Speaker.tone(1046, 110, 1, true);   // C6
  M5.Speaker.tone(1568, 160, 1, false);  // then G6
}

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

// The codec does one thing at a time: the speaker takes over from the mic.
static void speakerOn() {
  if (micOn) micStop([](const int16_t*, size_t) {});
  M5.Mic.end();
  if (!M5.Speaker.isEnabled()) M5.Speaker.begin();
}

bool playBegin(uint32_t rate, uint32_t holdMs) {
  rate = rate ? rate : 24000;
  bool fresh = freshClip || rate != playRate;
  // The answer's previous stream has all arrived and is still sounding:
  // carry straight on, so the two play back to back.
  if (!fresh && streamOpen && streamEnded) {
    streamEnded = false;
    carry = -1;
    return true;
  }
  playStop();
  speakerOn();
  if (fresh) {
    writePos = 0;
    cut = false;
    freshClip = false;
  }
  playRate = rate;
  playPos = writePos;
  carry = -1;
  streamOpen = true;
  streamEnded = false;
  started = false;
  holdUntilMs = millis() + holdMs;
  return false;
}

void playFeed(const uint8_t* data, size_t len) {
  if (!streamOpen || !playBuf) return;
  size_t i = 0;
  if (carry >= 0 && len) {
    if (writePos < kPlayCap) playBuf[writePos++] = shape((int16_t)(carry | (data[0] << 8)));
    carry = -1;
    i = 1;
  }
  size_t pairs = (len - i) / 2;
  size_t room = kPlayCap - writePos;
  size_t n = min(pairs, room);
  memcpy(playBuf + writePos, data + i, n * 2);  // little-endian on the ESP32 too
  if (boostGain != 1.0f) {
    for (size_t k = writePos; k < writePos + n; ++k) playBuf[k] = shape(playBuf[k]);
  }
  writePos += n;
  if ((len - i) % 2 && n == pairs) carry = data[len - 1];
}

void playEnd() {
  if (streamOpen) streamEnded = true;
}

void playStop() {
  if (M5.Speaker.isEnabled()) M5.Speaker.stop(0);
  if (streamOpen && !streamEnded) cut = true;  // the rest of it won't arrive
  streamOpen = false;
  streamEnded = false;
  started = false;
}

void playPoll() {
  if (!streamOpen) return;
  size_t avail = writePos - playPos;
  size_t queued = M5.Speaker.isPlaying(0);
  if (!started) {
    if ((int32_t)(millis() - holdUntilMs) < 0) return;  // the cue plays first
    // Buffer a little first so a slow network doesn't stutter the start.
    if (avail < playRate * 3 / 10 && !streamEnded) return;
    started = true;
  }
  if (queued < 2 && avail > 0 && (avail >= playRate / 10 || streamEnded)) {
    size_t n = min(avail, (size_t)(playRate / 2));
    if (queued == 0) {  // from silence: outputLevel() counts from here
      soundStartMs = millis();
      soundStartPos = playPos;
    }
    M5.Speaker.playRaw(playBuf + playPos, n, playRate, false, 1, 0, false);
    playPos += n;
  }
  if (streamEnded && playPos == writePos && M5.Speaker.isPlaying(0) == 0) {
    streamOpen = false;  // stream finished and fully played
  }
}

bool playing() { return streamOpen; }

bool sounding() { return streamOpen && started; }

int outputLevel() {
  // Where the speaker is now, assuming it has played without gaps since it
  // last started from silence (good enough to move a mouth).
  if (!sounding() || !playBuf) return 0;
  size_t at = soundStartPos + (size_t)((millis() - soundStartMs) * (uint64_t)playRate / 1000);
  if (at >= playPos) return 0;
  size_t win = playRate / 50;  // 20 ms
  size_t from = at > soundStartPos + win ? at - win : soundStartPos;
  int peak = 0;
  for (size_t i = from; i < at; i += 2) peak = max(peak, abs((int)playBuf[i]));
  return min(100, peak * 100 / 20000);
}

void newClip() { freshClip = true; }

bool hasClip() { return playBuf && writePos > 0 && !freshClip; }

bool clipCut() { return cut && !freshClip; }

void replay() {
  if (!hasClip()) return;
  bool arriving = streamOpen && !streamEnded;  // its last stream is still coming in
  if (M5.Speaker.isEnabled()) M5.Speaker.stop(0);
  speakerOn();
  playPos = 0;
  if (!arriving) carry = -1;
  streamOpen = true;
  streamEnded = !arriving;
  started = false;
  holdUntilMs = millis();
}

}  // namespace audio
