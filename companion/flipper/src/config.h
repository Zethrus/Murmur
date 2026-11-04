#pragma once

#include "protocol.h"

/**
 * The map shown on the device. Murmur is the source of truth and pushes it over the
 * link; the last copy is cached on the SD card so the app still shows something useful
 * while Murmur is closed.
 */
void config_load(MurmurMap* map);
void config_save(const MurmurMap* map);
