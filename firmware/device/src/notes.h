// Voice notes recorded while the device can't reach Flight Deck (no Wi-Fi,
// or the deck is down). Each is a WAV file (16 kHz mono), on the TF card when
// one is in (PaperColor), else in internal flash (LittleFS); once the deck is
// back they go up together and become one turn for the agent, marked as
// recorded offline (device_bridge.py).
//
// The PaperColor's card shares the SPI bus with the panel, which holds the
// bus for a whole redraw: callers touch the store only while ui::busy() is
// false (the main loop is the only one that starts a redraw).
#pragma once

#include <Arduino.h>

#include <vector>

namespace notes {

struct Note {
  String id;          // the file's name without ".wav": "<recorded epoch>-<random>"
  time_t when = 0;    // when it was recorded (0 = the clock wasn't set)
  size_t samples = 0; // 16-bit mono samples
};

// Mounts the card (or flash); false when this device keeps no notes.
bool begin();
// Where they're kept: "TF card", "flash", or "" (nowhere).
const char* where();
// The longest note there's room for now, in samples at `rate` (0 = no room).
// Kept up to date by begin/save/remove: no I/O, safe while the panel redraws.
size_t room(uint32_t rate);
// Keeps a recording; false when it doesn't fit or the write fails.
bool save(const int16_t* pcm, size_t samples, uint32_t rate, time_t when);
// The waiting notes, oldest first (at most `max`).
std::vector<Note> list(size_t max = 20);
// How many are waiting and their total length (kept up to date, no I/O).
int count();
uint32_t seconds();

// Reading one for the upload: open (gives its sample rate), read PCM bytes
// until it returns 0, close.
bool openRead(const Note& note, uint32_t* rate);
size_t read(uint8_t* buf, size_t max);
void closeRead();
// Gone for good (the deck has it).
void remove(const String& id);
// Set aside (renamed, never sent again): a file that can't be read as a note.
void quarantine(const String& id);

}  // namespace notes
