#include "protocol.h"

#include <stdio.h>
#include <string.h>

static const char* const button_names[MurmurButtonCount] = {"OK", "UP", "DOWN", "BACK", "LEFT", "RIGHT"};
static const char* const button_keys[MurmurButtonCount] = {"OK", "UP", "DOWN", "BACK", "LEFT", "RIGHT"};
static const char* const verb_names[] = {
    "none",
    "dictate_toggle",
    "enhance",
    "accept",
    "dismiss",
    "cancel",
};

const char* murmur_button_name(MurmurButton button) {
    if(button >= MurmurButtonCount) return "?";
    return button_names[button];
}

const char* murmur_verb_name(MurmurVerb verb) {
    if(verb > MurmurVerbCancel) return "none";
    return verb_names[verb];
}

const char* murmur_phase_text(MurmurPhase phase) {
    switch(phase) {
    case MurmurPhaseListen:
        return "Listening";
    case MurmurPhaseThink:
        return "Transcribing";
    case MurmurPhaseReady:
        return "Suggestion ready";
    case MurmurPhaseError:
        return "Murmur error";
    case MurmurPhaseOffline:
        return "Murmur offline";
    case MurmurPhaseIdle:
    default:
        return "Idle";
    }
}

const char* murmur_verb_wire(MurmurVerb verb) {
    static const char* const wire[] = {
        "NONE", "DICTATE_TOGGLE", "ENHANCE", "ACCEPT", "DISMISS", "CANCEL"};
    if(verb > MurmurVerbCancel) return "NONE";
    return wire[verb];
}

size_t murmur_build_cfg(const MurmurMap* map, char* out, size_t cap) {
    size_t used = (size_t)snprintf(out, cap, "CFG");
    for(size_t i = 0; i < MurmurButtonCount; i++) {
        if(used >= cap) break;
        used += (size_t)snprintf(out + used, cap - used, " %s=%s",
                                 button_keys[i], murmur_verb_wire(map->short_press[i]));
    }
    for(size_t i = 0; i < MurmurButtonCount; i++) {
        if(used >= cap) break;
        used += (size_t)snprintf(out + used, cap - used, " %s_LONG=%s",
                                 button_keys[i], murmur_verb_wire(map->long_press[i]));
    }
    return used < cap ? used : cap;
}

void murmur_map_default(MurmurMap* map) {
    for(size_t i = 0; i < MurmurButtonCount; i++) {
        map->short_press[i] = MurmurVerbNone;
        map->long_press[i] = MurmurVerbNone;
    }
    map->short_press[MurmurButtonOk] = MurmurVerbDictateToggle;
    map->short_press[MurmurButtonUp] = MurmurVerbEnhance;
    map->short_press[MurmurButtonDown] = MurmurVerbAccept;
    map->short_press[MurmurButtonBack] = MurmurVerbDismiss;
    map->long_press[MurmurButtonOk] = MurmurVerbCancel;
}

static MurmurVerb verb_from_wire(const char* value, size_t length) {
    static const char* const wire[] = {
        "NONE", "DICTATE_TOGGLE", "ENHANCE", "ACCEPT", "DISMISS", "CANCEL"};
    for(MurmurVerb verb = MurmurVerbNone; verb <= MurmurVerbCancel; verb++) {
        if(strlen(wire[verb]) == length && strncmp(wire[verb], value, length) == 0) {
            return verb;
        }
    }
    return MurmurVerbNone;
}

/** `KEY=VERB`, keys uppercase, underscore separates a long press. */
static void apply_pair(const char* pair, size_t length, MurmurMap* map) {
    const char* equals = memchr(pair, '=', length);
    if(!equals) return;
    size_t key_length = (size_t)(equals - pair);
    const char* value = equals + 1;
    size_t value_length = length - key_length - 1;
    if(key_length == 0 || value_length == 0) return;

    bool long_press = key_length > 5 && strncmp(pair + key_length - 5, "_LONG", 5) == 0;
    size_t base_length = long_press ? key_length - 5 : key_length;

    for(MurmurButton button = 0; button < MurmurButtonCount; button++) {
        if(strlen(button_keys[button]) != base_length) continue;
        if(strncmp(button_keys[button], pair, base_length) != 0) continue;
        MurmurVerb verb = verb_from_wire(value, value_length);
        if(long_press) {
            map->long_press[button] = verb;
        } else {
            map->short_press[button] = verb;
        }
        return;
    }
}

bool murmur_parse(const char* line, MurmurMessage* out) {
    memset(out, 0, sizeof(*out));
    if(!line || !*line) return false;

    if(strcmp(line, "P") == 0) {
        out->type = MurmurMessagePing;
        return true;
    }
    if(strncmp(line, "HELLO ", 6) == 0) {
        unsigned protocol = 0;
        char app[sizeof(out->app)] = {0};
        if(sscanf(line + 6, "%u %23s", &protocol, app) < 1) return false;
        out->type = MurmurMessageHello;
        out->protocol = (uint8_t)protocol;
        strncpy(out->app, app, sizeof(out->app) - 1);
        return true;
    }
    if(strncmp(line, "S ", 2) == 0) {
        const char* value = line + 2;
        out->type = MurmurMessageState;
        if(strncmp(value, "LISTEN", 6) == 0) out->phase = MurmurPhaseListen;
        else if(strncmp(value, "THINK", 5) == 0) out->phase = MurmurPhaseThink;
        else if(strncmp(value, "READY", 5) == 0) out->phase = MurmurPhaseReady;
        else if(strncmp(value, "ERROR", 5) == 0) out->phase = MurmurPhaseError;
        else out->phase = MurmurPhaseIdle;
        return true;
    }
    if(strncmp(line, "A ", 2) == 0) {
        out->type = MurmurMessageAck;
        strncpy(out->app, line + 2, sizeof(out->app) - 1);
        return true;
    }
    if(strncmp(line, "CFG ", 4) == 0) {
        out->type = MurmurMessageConfig;
        murmur_map_default(&out->map);
        const char* cursor = line + 4;
        while(*cursor) {
            while(*cursor == ' ') cursor++;
            if(!*cursor) break;
            const char* end = strchr(cursor, ' ');
            size_t length = end ? (size_t)(end - cursor) : strlen(cursor);
            apply_pair(cursor, length, &out->map);
            if(!end) break;
            cursor = end + 1;
        }
        return true;
    }
    return false;
}

void murmur_hello(char* out, size_t cap, const char* version) {
    snprintf(out, cap, "HELLO %d %s flipper", MURMUR_PROTOCOL_VERSION, version);
}
