#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/** Wire protocol revision. Mirrored in src-tauri/src/companion/protocol.rs. */
#define MURMUR_PROTOCOL_VERSION 4
/** 220 wire bytes (including newline), plus the C string terminator. */
#define MURMUR_MAX_LINE 221
/** 217 UTF-8 payload bytes, plus the C string terminator. */
#define MURMUR_TEXT_MAX 218

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

typedef enum {
    MurmurTransportNone = 0,
    MurmurTransportUsb,
    MurmurTransportBle,
} MurmurTransport;

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
    MurmurMessagePtt,
    MurmurMessagePing,
    MurmurMessageText,
    MurmurMessageSuggestion,
    MurmurMessageLink,
} MurmurMessageType;

typedef struct {
    MurmurMessageType type;
    uint8_t protocol;
    char app[24];
    MurmurPhase phase;
    MurmurMap map;
    bool ptt;
    /** Payload of a T (live text) or G (suggestion) line. */
    char text[MURMUR_TEXT_MAX];
    /** Latency in ms and host-side RSSI from an L line. */
    uint16_t latency_ms;
    int8_t rssi;
} MurmurMessage;

const char* murmur_button_name(MurmurButton button);
const char* murmur_verb_name(MurmurVerb verb);
const char* murmur_verb_wire(MurmurVerb verb);
const char* murmur_transport_name(MurmurTransport transport);
/** Serialise the map as a CFG line. Returns the written length. */
size_t murmur_build_cfg(const MurmurMap* map, char* out, size_t cap);
const char* murmur_phase_text(MurmurPhase phase);
void murmur_map_default(MurmurMap* map);
/** Parse one line (no terminator). Unknown lines yield MurmurMessageNone. */
bool murmur_parse(const char* line, MurmurMessage* out);
void murmur_hello(char* out, size_t cap, const char* version);
/** Name a phase transition for the vibration cue. */
bool murmur_phase_is_quiet(MurmurPhase phase);
