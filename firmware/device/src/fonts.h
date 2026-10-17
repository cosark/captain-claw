// Smooth (antialiased) fonts for the round screen: Inter, rendered to VLW by
// tools/make_fonts.py into fonts/*.vlw and embedded in the firmware.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace vlw {

enum Id : uint8_t {
  Small,   // 22 px medium: clock, labels, hints
  BodyS,   // 27 px: reading text, small
  BodyM,   // 32 px: reading text, medium
  BodyL,   // 39 px: reading text, large
  Title,   // 34 px semibold: names under badges, screen titles
  Badge,   // 58 px bold: the text in a badge (no small accented letters)
  Digits,  // 104 px medium: timers (digits, ":" "." "-" only)
  Count
};

// The font file's bytes and length.
const uint8_t* data(Id id, size_t* len);

}  // namespace vlw
