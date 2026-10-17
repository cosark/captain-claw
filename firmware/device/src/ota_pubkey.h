// Public half of the firmware signing key (tools/publish.py keygen). The
// private half stays on the publishing Mac; a build signed with anything else
// is refused by the over-the-air updater.
#pragma once

static const char kOtaPublicKey[] =
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEz0H0QOGA+6pnBjy//XgVWmUvV8Jh\n"
    "MnMqwMbjPBqYbnhn87a6pN5q4PzVHJ7JZlqU+IbdcM4kA3B7tpgQmDjVrA==\n"
    "-----END PUBLIC KEY-----\n";
