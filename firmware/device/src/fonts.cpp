// The fonts embedded by platformio.ini (board_build.embed_files): the linker
// names each file _binary_<path>_start / _end.
#include "fonts.h"

#define VLW_FILE(name)                                                       \
  extern const uint8_t name##_start[] asm("_binary_fonts_" #name "_vlw_start"); \
  extern const uint8_t name##_end[] asm("_binary_fonts_" #name "_vlw_end");

VLW_FILE(small)
VLW_FILE(body_s)
VLW_FILE(body_m)
VLW_FILE(body_l)
VLW_FILE(title)
VLW_FILE(badge)
VLW_FILE(digits)

namespace vlw {

const uint8_t* data(Id id, size_t* len) {
  static const uint8_t* const starts[Count] = {small_start, body_s_start, body_m_start, body_l_start,
                                               title_start, badge_start, digits_start};
  static const uint8_t* const ends[Count] = {small_end, body_s_end, body_m_end, body_l_end,
                                             title_end, badge_end, digits_end};
  *len = ends[id] - starts[id];
  return starts[id];
}

}  // namespace vlw
