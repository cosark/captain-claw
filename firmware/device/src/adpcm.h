// IMA ADPCM: 4 bits a sample, a quarter of 16-bit PCM. Offline voice notes
// are kept this way at 8 kHz (notes.cpp): 4 KB a second, telephone quality,
// which is as low as a note can go and still transcribe well (2-bit codes or
// a lower rate start losing words). Plain C++, so the Mac checks it too.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace adpcm {

static const int16_t kSteps[89] = {
    7,     8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,    25,    28,
    31,    34,    37,    41,    45,    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,
    130,   143,   157,   173,   190,   209,   230,   253,   279,   307,   337,   371,   408,   449,   494,
    544,   598,   658,   724,   796,   876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
    2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,
    9493,  10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767};
static const int8_t kIndexStep[8] = {-1, -1, -1, -1, 2, 4, 6, 8};

// The coder's state; encoder and decoder each keep one, both starting at zero.
struct State {
  int32_t predictor = 0;
  int32_t index = 0;
};

static inline int16_t clamp16(int32_t v) { return v > 32767 ? 32767 : v < -32768 ? -32768 : (int16_t)v; }

// One code (4 bits) back to a sample.
static inline int16_t decode(State& s, uint8_t code) {
  const int32_t step = kSteps[s.index];
  int32_t diff = step >> 3;
  if (code & 4) diff += step;
  if (code & 2) diff += step >> 1;
  if (code & 1) diff += step >> 2;
  s.predictor = clamp16(code & 8 ? s.predictor - diff : s.predictor + diff);
  s.index += kIndexStep[code & 7];
  s.index = s.index < 0 ? 0 : s.index > 88 ? 88 : s.index;
  return (int16_t)s.predictor;
}

// One sample to a code; runs the decoder too, so both sides stay in step.
static inline uint8_t encode(State& s, int16_t sample) {
  const int32_t step = kSteps[s.index];
  int32_t diff = (int32_t)sample - s.predictor;
  uint8_t code = 0;
  if (diff < 0) {
    code = 8;
    diff = -diff;
  }
  if (diff >= step) code |= 4, diff -= step;
  if (diff >= step >> 1) code |= 2, diff -= step >> 1;
  if (diff >= step >> 2) code |= 1;
  decode(s, code);
  return code;
}

// 16 kHz → 8 kHz: sample `i` of the halved signal, through a half-band
// low-pass (flat to 3.4 kHz, −30 dB from 4.6 kHz) so the hiss above 4 kHz
// doesn't fold back into the voice. A half-band filter's even taps are zero.
static inline int16_t halved(const int16_t* in, size_t n, size_t i) {
  static const int16_t kTaps[10] = {10344, -3226, 1690, -980, 570, -318, 163, -73, 26, -5};  // Q15, odd offsets
  const long c = (long)(2 * i);
  int32_t acc = (c < (long)n ? in[c] : 0) * 16386;
  for (int k = 0; k < 10; ++k) {
    const long d = 2 * k + 1;
    const int32_t a = c - d >= 0 ? in[c - d] : 0;
    const int32_t b = c + d < (long)n ? in[c + d] : 0;
    acc += (a + b) * kTaps[k];
  }
  return clamp16((acc + 16384) >> 15);
}

}  // namespace adpcm
