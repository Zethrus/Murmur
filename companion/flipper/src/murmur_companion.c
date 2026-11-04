#include "config.h"
#include "link.h"
#include "protocol.h"
#include "version.h"

#include <furi.h>
#include <furi_hal.h>
#include <gui/gui.h>
#include <gui/view_port.h>
#include <input/input.h>
#include <notification/notification.h>
#include <notification/notification_messages.h>

#include <stdio.h>
#include <string.h>

#define TICK_MS 25
#define HELLO_RETRY_MS 2000
#define HEARTBEAT_MS 5000
#define LINK_TIMEOUT_MS 15000
#define BATTERY_MS 30000

#define LIVE_MAX (MURMUR_TEXT_MAX * 2)
#define TEXT_LINES 6
#define TEXT_COLS 21
#define HISTORY_SLOTS 3
#define HISTORY_CHARS 40

static const NotificationSequence sequence_ready_vibro = {
    &message_vibro_on, &message_delay_100, &message_vibro_off,
    &message_delay_100, &message_vibro_on, &message_delay_100,
    &message_vibro_off, NULL,
};

typedef struct {
    Gui* gui;
    ViewPort* view_port;
    FuriMessageQueue* queue;
    MurmurLink* link;
    NotificationApp* notifications;

    MurmurMap map;
    MurmurPhase phase;
    bool connected;
    bool negotiated;
    bool ptt;
    bool discard_prompt;
    bool suppress_ok_release;
    uint8_t peer_protocol;
    char peer_app[24];
    char last_action[24];
    char notice[32];

    /* Live dictation text and the enhancement suggestion. RAM only, never persisted. */
    char live[LIVE_MAX];
    char suggestion[LIVE_MAX];
    uint8_t suggestion_scroll;
    char history[HISTORY_SLOTS][HISTORY_CHARS + 1];
    uint8_t history_next;
    uint8_t history_count;
    int8_t history_view;
    bool history_finalizing;
    bool history_has_current;
    bool discarded_turn;

    uint16_t latency_ms;
    int8_t rssi;
    uint8_t battery;

    uint32_t started_at;
    uint32_t last_rx;
    uint32_t last_hello;
    uint32_t last_ping;
    uint32_t last_battery;
    bool running;
} MurmurApp;

static const InputKey action_keys[] = {
    InputKeyOk,
    InputKeyUp,
    InputKeyDown,
    InputKeyBack,
    InputKeyLeft,
    InputKeyRight,
};

static bool key_to_button(InputKey key, MurmurButton* button) {
    for(size_t i = 0; i < COUNT_OF(action_keys); i++) {
        if(action_keys[i] == key) {
            *button = (MurmurButton)i;
            return true;
        }
    }
    return false;
}

static void input_callback(InputEvent* event, void* context) {
    MurmurApp* app = context;
    furi_message_queue_put(app->queue, event, 0);
}

/** Word wrap into at most `max_lines` rows of `cols` characters. Returns the row count. */
static size_t wrap_lines(const char* text, char rows[TEXT_LINES][TEXT_COLS + 1], size_t max_lines,
                         bool keep_tail) {
    size_t count = 0;
    size_t length = strlen(text);
    size_t cursor = 0;

    while(cursor < length && count < max_lines) {
        size_t remaining = length - cursor;
        size_t take = remaining < TEXT_COLS ? remaining : TEXT_COLS;
        if(take < remaining) {
            /* Prefer a break at the last space inside the window. */
            size_t space = take;
            while(space > 0 && text[cursor + space] != ' ') space--;
            if(space > 0) take = space;
        }
        memcpy(rows[count], text + cursor, take);
        rows[count][take] = '\0';
        count++;
        cursor += take;
        while(cursor < length && text[cursor] == ' ') cursor++;
    }

    if(keep_tail && cursor < length && count) {
        /* Keep the newest words visible: re-wrap the tail instead of the head. */
        size_t start = length > (size_t)(TEXT_COLS * max_lines) ?
                           length - (size_t)(TEXT_COLS * max_lines) :
                           0;
        while(start < length && text[start] != ' ') start++;
        if(start < length) return wrap_lines(text + start, rows, max_lines, false);
    }
    return count;
}

static void draw_text_block(Canvas* canvas, const char* text, int16_t top, bool keep_tail) {
    char rows[TEXT_LINES][TEXT_COLS + 1];
    size_t count = wrap_lines(text, rows, TEXT_LINES, keep_tail);
    for(size_t i = 0; i < count; i++) {
        if(rows[i][0] == '\0') continue;
        canvas_draw_str(canvas, 2, (int16_t)(top + 7 + (int16_t)i * 8), rows[i]);
    }
}

static void draw_callback(Canvas* canvas, void* context) {
    MurmurApp* app = context;
    canvas_clear(canvas);
    canvas_set_font(canvas, FontSecondary);

    /* Header: identity on the left, the three link indicators on the right. */
    canvas_draw_str(canvas, 2, 9, "MURMUR");
    char header[24];
    if(app->connected && app->negotiated) {
        snprintf(header, sizeof(header), "%c %u%% %ums", 
                 link_transport(app->link) == MurmurTransportUsb ? 'U' : 'B',
                 app->battery, app->latency_ms);
    } else if(app->connected) {
        snprintf(header, sizeof(header), "v%u", app->peer_protocol);
    } else {
        snprintf(header, sizeof(header), "offline");
    }
    canvas_draw_str_aligned(canvas, 126, 9, AlignRight, AlignBottom, header);
    canvas_draw_line(canvas, 0, 11, 127, 11);

    if(app->discard_prompt) {
        canvas_draw_str_aligned(canvas, 64, 30, AlignCenter, AlignCenter, "Discard dictation?");
        canvas_draw_line(canvas, 0, 52, 127, 52);
        canvas_draw_str(canvas, 2, 62, "OK discard");
        canvas_draw_str_aligned(canvas, 126, 62, AlignRight, AlignBottom, "BACK keep");
        return;
    }

    if(app->phase == MurmurPhaseIdle && app->history_view >= 0) {
        uint8_t slot = (app->history_next + HISTORY_SLOTS - 1 - (uint8_t)app->history_view) % HISTORY_SLOTS;
        draw_text_block(canvas, app->history[slot], 12, false);
        canvas_draw_line(canvas, 0, 52, 127, 52);
        char index[8];
        snprintf(index, sizeof(index), "%u/%u", (unsigned)app->history_view + 1, (unsigned)app->history_count);
        canvas_draw_str(canvas, 2, 62, index);
        canvas_draw_str_aligned(canvas, 126, 62, AlignRight, AlignBottom, "DN accept");
        return;
    }

    if(app->suggestion[0]) {
        char rows[TEXT_LINES][TEXT_COLS + 1];
        size_t count = wrap_lines(app->suggestion, rows, TEXT_LINES, false);
        for(size_t i = app->suggestion_scroll;
            i < count && i < (size_t)app->suggestion_scroll + 4;
            i++) {
            canvas_draw_str(canvas, 2, (int16_t)(20 + (int16_t)(i - app->suggestion_scroll) * 9),
                            rows[i]);
        }
        canvas_draw_line(canvas, 0, 52, 127, 52);
        canvas_draw_str(canvas, 2, 62, "DN accept");
        canvas_draw_str_aligned(canvas, 126, 62, AlignRight, AlignBottom, "BK dismiss");
        return;
    }

    if(app->live[0]) {
        draw_text_block(canvas, app->live, 12, true);
    } else {
        canvas_draw_str_aligned(canvas, 64, 30, AlignCenter, AlignCenter,
                                murmur_phase_text(app->phase));
        if(app->notice[0]) {
            canvas_draw_str_aligned(canvas, 64, 42, AlignCenter, AlignCenter, app->notice);
        } else if(app->last_action[0]) {
            canvas_draw_str_aligned(canvas, 64, 42, AlignCenter, AlignCenter, app->last_action);
        }
    }

    canvas_draw_line(canvas, 0, 52, 127, 52);
    canvas_draw_str(canvas, 2, 62, murmur_phase_text(app->phase));
    char footer[40];
    if(app->notice[0] && app->live[0]) {
        snprintf(footer, sizeof(footer), "%s", app->notice);
    } else if(app->last_action[0]) {
        snprintf(footer, sizeof(footer), "%s", app->last_action);
    } else {
        footer[0] = '\0';
    }
    if(footer[0]) canvas_draw_str_aligned(canvas, 126, 62, AlignRight, AlignBottom, footer);
}

static void send_line(MurmurApp* app, const char* line) {
    if(!app->connected) {
        snprintf(app->notice, sizeof(app->notice), "Not connected");
        return;
    }
    link_send(app->link, line);
}

static void vibrate(MurmurApp* app) {
    notification_message(app->notifications, &sequence_single_vibro);
}

static void remember_text(MurmurApp* app, bool replace) {
    if(!app->live[0]) return;
    uint8_t slot = replace ? (app->history_next + HISTORY_SLOTS - 1) % HISTORY_SLOTS : app->history_next;
    char* entry = app->history[slot];
    size_t n = strcspn(app->live, "\r\n");
    if(n > HISTORY_CHARS) n = HISTORY_CHARS;
    memcpy(entry, app->live, n);
    entry[n] = '\0';
    if(!replace) {
        app->history_next = (app->history_next + 1) % HISTORY_SLOTS;
        if(app->history_count < HISTORY_SLOTS) app->history_count++;
    }
    app->history_view = -1;
}

static void handle_press(MurmurApp* app, MurmurButton button, bool long_press) {
    MurmurVerb verb = long_press ? app->map.long_press[button] : app->map.short_press[button];
    if(verb == MurmurVerbNone) {
        snprintf(app->notice, sizeof(app->notice), "Unmapped");
        return;
    }
    app->notice[0] = '\0';
    snprintf(app->last_action, sizeof(app->last_action), "%s -> %s",
             murmur_button_name(button), murmur_verb_name(verb));
    char line[32];
    snprintf(line, sizeof(line), "B %s%s", murmur_button_name(button), long_press ? " LONG" : "");
    send_line(app, line);
}

/** Suggestion view takes Left/Right for scrolling and clears on accept or dismiss. */
static bool handle_suggestion_key(MurmurApp* app, MurmurButton button, bool long_press) {
    if(!app->suggestion[0]) return false;
    MurmurVerb verb = long_press ? app->map.long_press[button] : app->map.short_press[button];
    bool consumed = false;
    switch(button) {
    case MurmurButtonLeft:
        if(app->suggestion_scroll > 0) app->suggestion_scroll--;
        consumed = true;
        break;
    case MurmurButtonRight:
        app->suggestion_scroll++;
        consumed = true;
        break;
    default:
        if(verb == MurmurVerbAccept || verb == MurmurVerbDismiss) {
            app->suggestion[0] = '\0';
            app->suggestion_scroll = 0;
        }
        break;
    }
    UNUSED(long_press);
    return consumed;
}

static void handle_message(MurmurApp* app, const char* line) {
    MurmurMessage message;
    if(!murmur_parse(line, &message)) return;
    app->last_rx = furi_get_tick();

    switch(message.type) {
    case MurmurMessageHello: {
        app->peer_protocol = message.protocol;
        strncpy(app->peer_app, message.app, sizeof(app->peer_app) - 1);
        app->negotiated = (message.protocol == MURMUR_PROTOCOL_VERSION);
        /* Reply on the incoming link even when another transport is connected. */
        char hello[MURMUR_MAX_LINE];
        murmur_hello(hello, sizeof(hello), MURMUR_COMPANION_VERSION);
        link_send_reply(app->link, hello);
        if(!app->negotiated) {
            snprintf(app->notice, sizeof(app->notice), "Update the app");
        }
        break;
    }
    case MurmurMessageConfig:
        app->map = message.map;
        config_save(&app->map);
        break;
    case MurmurMessageState:
        if(message.phase != MurmurPhaseListen) app->discard_prompt = false;
        if(app->phase == MurmurPhaseListen && message.phase != MurmurPhaseListen && !app->discarded_turn) {
            app->history_finalizing = true;
            app->history_has_current = app->live[0] != '\0';
            remember_text(app, false);
        }
        if(app->phase != message.phase && !murmur_phase_is_quiet(message.phase)) {
            notification_message(app->notifications,
                                 message.phase == MurmurPhaseReady ? &sequence_ready_vibro : &sequence_single_vibro);
        }
        app->phase = message.phase;
        if(message.phase == MurmurPhaseIdle || message.phase == MurmurPhaseListen || message.phase == MurmurPhaseReady) {
            app->live[0] = '\0';
            if(message.phase == MurmurPhaseListen) {
                app->discarded_turn = false;
                app->history_finalizing = false;
                app->history_has_current = false;
                app->suggestion[0] = '\0';
                app->history_view = -1;
            }
        }
        break;
    case MurmurMessagePtt:
        app->ptt = message.ptt;
        break;
    case MurmurMessageAck:
        strncpy(app->last_action, message.app, sizeof(app->last_action) - 1);
        if(strcmp(message.app, "ACCEPT UNAVAILABLE") == 0) {
            app->history_view = -1;
            snprintf(app->notice, sizeof(app->notice), "Unavailable");
        }
        break;
    case MurmurMessageText:
        if(app->discarded_turn) break;
        strncpy(app->live, message.text, sizeof(app->live) - 1);
        app->live[sizeof(app->live) - 1] = '\0';
        if(app->history_finalizing && app->live[0]) {
            remember_text(app, app->history_has_current);
            app->history_has_current = true;
            if(app->phase == MurmurPhaseIdle || app->phase == MurmurPhaseReady) app->live[0] = '\0';
        }
        break;
    case MurmurMessageSuggestion:
        strncpy(app->suggestion, message.text, sizeof(app->suggestion) - 1);
        app->suggestion[sizeof(app->suggestion) - 1] = '\0';
        app->suggestion_scroll = 0;
        app->history_view = -1;
        break;
    case MurmurMessageLink:
        app->latency_ms = message.latency_ms;
        app->rssi = message.rssi;
        break;
    case MurmurMessagePing:
        /* A bare P is presence and is never answered. A P with a token is a probe and is
           echoed once, which is what the host times for its latency reading. */
        if(message.text[0]) {
            char echo[MURMUR_MAX_LINE];
            snprintf(echo, sizeof(echo), "P %s", message.text);
            link_send_reply(app->link, echo);
        }
        break;
    case MurmurMessageNone:
    default:
        break;
    }
}

static void service_link(MurmurApp* app) {
    char line[MURMUR_MAX_LINE];
    for(uint8_t i = 0; i < 8 && link_read_line(app->link, line, sizeof(line)); i++) {
        handle_message(app, line);
    }
    link_service(app->link);
}

static void service_timers(MurmurApp* app) {
    uint32_t now = furi_get_tick();
    if(!app->connected) {
        app->phase = MurmurPhaseOffline;
        app->discard_prompt = false;
        return;
    }
    if(!app->negotiated && now - app->last_hello >= HELLO_RETRY_MS) {
        char hello[MURMUR_MAX_LINE];
        murmur_hello(hello, sizeof(hello), MURMUR_COMPANION_VERSION);
        link_send(app->link, hello);
        app->last_hello = now;
    }
    if(now - app->last_ping >= HEARTBEAT_MS) {
        link_send(app->link, "P");
        app->last_ping = now;
    }
    if(now - app->last_battery >= BATTERY_MS) {
        app->battery = (uint8_t)furi_hal_power_get_pct();
        char battery[16];
        snprintf(battery, sizeof(battery), "Q %u", app->battery);
        link_send(app->link, battery);
        app->last_battery = now;
    }
    if(app->last_rx != 0 && now - app->last_rx > LINK_TIMEOUT_MS) {
        app->negotiated = false;
        app->phase = MurmurPhaseOffline;
        snprintf(app->notice, sizeof(app->notice), "Link timed out");
    }
}

int32_t murmur_companion_app(void* p) {
    UNUSED(p);
    MurmurApp* app = malloc(sizeof(MurmurApp));
    memset(app, 0, sizeof(MurmurApp));
    app->running = true;
    app->phase = MurmurPhaseOffline;
    app->history_view = -1;
    strncpy(app->notice, "Waiting for Murmur", sizeof(app->notice) - 1);

    config_load(&app->map);
    app->link = link_alloc();
    app->queue = furi_message_queue_alloc(8, sizeof(InputEvent));
    app->notifications = furi_record_open(RECORD_NOTIFICATION);

    app->gui = furi_record_open(RECORD_GUI);
    app->view_port = view_port_alloc();
    view_port_draw_callback_set(app->view_port, draw_callback, app);
    view_port_input_callback_set(app->view_port, input_callback, app);
    gui_add_view_port(app->gui, app->view_port, GuiLayerFullscreen);

    /* USB first: it is the wired path and never needs the radio. BLE is additive. */
    bool link_ready = link_open(app->link);
    if(!link_ready) {
        snprintf(app->notice, sizeof(app->notice), "USB port busy");
    }
    if(!link_open_ble(app->link)) {
        FURI_LOG_W("Murmur", "BLE unavailable");
    }
    app->started_at = furi_get_tick();
    app->last_hello = app->started_at - HELLO_RETRY_MS;
    app->battery = (uint8_t)furi_hal_power_get_pct();

    while(app->running) {
        InputEvent event;
        if(furi_message_queue_get(app->queue, &event, TICK_MS) == FuriStatusOk) {
            MurmurButton button;
            if(app->discard_prompt) {
                if(event.key == InputKeyOk && event.type == InputTypeShort) {
                    app->discard_prompt = false;
                    app->suppress_ok_release = true;
                    if(app->connected && link_send(app->link, "D DISCARD")) {
                        app->discarded_turn = true;
                        app->history_finalizing = false;
                        app->history_has_current = false;
                        app->live[0] = '\0';
                        app->phase = MurmurPhaseIdle;
                    } else {
                        snprintf(app->notice, sizeof(app->notice), "Not sent");
                    }
                } else if(event.key == InputKeyBack && event.type == InputTypeShort) {
                    app->discard_prompt = false;
                }
            } else if(event.key == InputKeyBack && event.type == InputTypeLong) {
                if(app->phase == MurmurPhaseListen) app->discard_prompt = true;
                else app->running = false;
            } else if(app->ptt && event.key == InputKeyOk &&
                      (event.type == InputTypePress || event.type == InputTypeRelease)) {
                if(event.type == InputTypePress) {
                    app->suppress_ok_release = false;
                    send_line(app, "B OK PRESS");
                    vibrate(app);
                } else if(app->suppress_ok_release) {
                    app->suppress_ok_release = false;
                } else {
                    send_line(app, "B OK RELEASE");
                }
            } else if(app->ptt && event.key == InputKeyOk) {
                /* The short/long events follow PRESS and must not toggle again. */
            } else if(event.type == InputTypeShort || event.type == InputTypeLong) {
                if(key_to_button(event.key, &button)) {
                    bool long_press = event.type == InputTypeLong;
                    if(app->phase == MurmurPhaseIdle && !app->suggestion[0] && app->history_count && !long_press &&
                       (button == MurmurButtonLeft || button == MurmurButtonRight)) {
                        if(app->history_view < 0) app->history_view = 0;
                        else app->history_view = (app->history_view +
                            (button == MurmurButtonRight ? 1 : app->history_count - 1)) % app->history_count;
                    } else if(app->phase == MurmurPhaseIdle && app->history_view >= 0 &&
                              button == MurmurButtonDown && !long_press) {
                        char request[24];
                        snprintf(request, sizeof(request), "D ACCEPT %u", (unsigned)app->history_view);
                        send_line(app, request);
                    } else if(!handle_suggestion_key(app, button, long_press)) {
                        handle_press(app, button, long_press);
                    }
                    vibrate(app);
                }
            }
        }
        bool connected = link_connected(app->link);
        if(connected && !app->connected) {
            app->last_rx = furi_get_tick();
            app->last_hello = 0;
            app->notice[0] = '\0';
        } else if(!connected && app->connected) {
            app->negotiated = false;
            snprintf(app->notice, sizeof(app->notice), "Link disconnected");
        }
        app->connected = connected;
        if(app->last_rx == 0) app->last_rx = furi_get_tick();

        service_link(app);
        service_timers(app);

        view_port_update(app->view_port);
    }

    link_close(app->link);
    gui_remove_view_port(app->gui, app->view_port);
    view_port_free(app->view_port);
    furi_record_close(RECORD_GUI);
    furi_record_close(RECORD_NOTIFICATION);
    furi_message_queue_free(app->queue);
    link_free(app->link);
    free(app);
    return 0;
}
