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
void cue();   // two-tone "about to speak" chime

// ── microphone ──
void micStart();
// Each finished block of 16 kHz mono samples goes to `sink` (called from loop()).
// Returns the last block's peak level 0..100 (for the on-screen meter).
int micPoll(void (*sink)(const int16_t* samples, size_t count));
// Stops recording and flushes the blocks still in flight to `sink`.
void micStop(void (*sink)(const int16_t* samples, size_t count));
bool micActive();

// ── speaker (one stream at a time, PCM s16le mono) ──
// The streams of one answer (its speech, then any sound the agent made) are
// kept back to back as the answer's clip, which replay() plays again.
//
// A new stream; sound starts no earlier than holdMs from now — room for the
// beep/flash cue before speech. Returns true when it carries straight on from
// the answer's previous stream, which is still sounding (no cue then).
bool playBegin(uint32_t rate, uint32_t holdMs = 0);
void playFeed(const uint8_t* data, size_t len);
void playEnd();                            // no more data for this stream
void playStop();                           // stop now (the clip is kept)
void playPoll();                           // keep the speaker fed; call from loop()
bool playing();                            // a stream is buffered or sounding
bool sounding();                           // the speaker is actually outputting it
int outputLevel();                         // 0..100 loudness of what's sounding now

// ── the last answer's clip ──
void newClip();   // a new answer: its first stream replaces the clip
bool hasClip();   // there is sound to replay
bool clipCut();   // a stream stopped arriving before its end (stopped, link lost)
void replay();    // play the clip again from its start, at once (no cue)

}  // namespace audio
