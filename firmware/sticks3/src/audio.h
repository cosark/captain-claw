// Microphone capture and streamed speaker playback.
// The StickS3's ES8311 codec does one at a time: starting either stops the other.
#pragma once

#include <Arduino.h>

namespace audio {

static constexpr uint32_t kMicRate = 16000;

void begin(uint8_t volume, uint8_t boostDb);
void setVolume(uint8_t volume);
// Extra gain on speech with a soft limiter (peaks are rounded, not clipped).
void setBoost(uint8_t db);
void beep();  // short tone at the current volume (feedback for volume changes)

// ── microphone ──
void micStart();
// Each finished block of 16 kHz mono samples goes to `sink` (called from loop()).
// Returns the last block's peak level 0..100 (for the on-screen meter).
int micPoll(void (*sink)(const int16_t* samples, size_t count));
// Stops recording and flushes the blocks still in flight to `sink`.
void micStop(void (*sink)(const int16_t* samples, size_t count));
bool micActive();

// ── speaker (one stream at a time, PCM s16le mono) ──
void playBegin(uint32_t rate);             // a new stream (drops any old one)
void playFeed(const uint8_t* data, size_t len);
void playEnd();                            // no more data for this stream
void playStop();                           // stop now, drop everything
void playPoll();                           // keep the speaker fed; call from loop()
bool playing();                            // a stream is buffered or sounding

}  // namespace audio
