// Voice notes recorded while the device can't reach Flight Deck (no Wi-Fi,
// or the deck is down), kept in internal flash (LittleFS) as 8 kHz 4-bit
// ADPCM (adpcm.h): 4 KB a second, so the flash holds about a quarter of an
// hour. Once the deck is back — and the user said yes — they go up together
// as plain PCM and become one turn for the agent, marked as recorded offline
// (device_bridge.py). Notes kept by older firmware (16 kHz WAV) still go up.
#pragma once

#include <Arduino.h>

#include <vector>

namespace notes {

struct Note {
  String id;          // the file's name without its extension: "<recorded epoch>-<random>"
  time_t when = 0;    // when it was recorded (0 = the clock wasn't set)
  size_t samples = 0; // its length, in samples at `rate`
  uint32_t rate = 8000;
};

// Mounts the flash; false when this device keeps no notes.
bool begin();
// Notes can be kept (the flash is mounted).
bool ready();
// The longest recording at `rate` (the mic's) there's room for now, in
// samples (0 = no room). Kept up to date by begin/save/remove: no I/O.
size_t room(uint32_t rate);
// Keeps a recording (packed down as it's written); false when it doesn't fit
// or the write fails.
bool save(const int16_t* pcm, size_t samples, uint32_t rate, time_t when);
// The waiting notes, oldest first (at most `max`).
std::vector<Note> list(size_t max = 20);
// How many are waiting and their total length (kept up to date, no I/O).
int count();
uint32_t seconds();

// Reading one for the upload: open (gives its sample rate), read 16-bit PCM
// bytes until it returns 0, close.
bool openRead(const Note& note, uint32_t* rate);
size_t read(uint8_t* buf, size_t max);
void closeRead();
// Gone for good (the deck has it).
void remove(const String& id);
// Set aside (renamed, never sent again): a file that can't be read as a note.
void quarantine(const String& id);

}  // namespace notes
