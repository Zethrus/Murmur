#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/** Wire protocol revision. Mirrored in src-tauri/src/companion/protocol.rs. */
#define MURMUR_PROTOCOL_VERSION 1
/** Longest line, including the terminator. */
#define MURMUR_MAX_LINE 160

typedef enum {
    MurmurVerbNone = 0,
    MurmurVerbDictateToggle,
    MurmurVerbEnhance,
    MurmurVerbAccept,
    MurmurVerbDismiss,
    MurmurVerbCancel,
} MurmurVerb;

typedef enum {
    MurmurButtonOk = 0,
    MurmurButtonUp,
    MurmurButtonDown,
    MurmurButtonBack,
    MurmurButtonLeft,
    MurmurButtonRight,
    MurmurButtonCount,
} MurmurButton;

typedef enum {
    MurmurPhaseIdle = 0,
    MurmurPhaseListen,
    MurmurPhaseThink,
    MurmurPhaseReady,
    MurmurPhaseError,
    MurmurPhaseOffline,
} MurmurPhase;

typedef struct {
    MurmurVerb short_press[MurmurButtonCount];
    MurmurVerb long_press[MurmurButtonCount];
} MurmurMap;

typedef enum {
    MurmurMessageNone = 0,
    MurmurMessageHello,
    MurmurMessageState,
    MurmurMessageAck,
    MurmurMessageConfig,
    MurmurMessagePing,
} MurmurMessageType;

typedef struct {
    MurmurMessageType type;
    uint8_t protocol;
    char app[24];
    MurmurPhase phase;
    MurmurMap map;
} MurmurMessage;

const char* murmur_button_name(MurmurButton button);
const char* murmur_verb_name(MurmurVerb verb);
const char* murmur_verb_wire(MurmurVerb verb);
/** Serialise the map as a CFG line. Returns the written length. */
size_t murmur_build_cfg(const MurmurMap* map, char* out, size_t cap);
const char* murmur_phase_text(MurmurPhase phase);
void murmur_map_default(MurmurMap* map);
/** Parse one line (no terminator). Unknown lines yield MurmurMessageNone. */
bool murmur_parse(const char* line, MurmurMessage* out);
void murmur_hello(char* out, size_t cap, const char* version);
