#include "config.h"
#include "link.h"
#include "protocol.h"
#include "version.h"

#include <furi.h>
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
    uint8_t peer_protocol;
    char peer_app[24];
    char last_action[24];
    char notice[32];

    uint32_t started_at;
    uint32_t last_rx;
    uint32_t last_hello;
    uint32_t last_ping;
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

static void draw_callback(Canvas* canvas, void* context) {
    MurmurApp* app = context;
    canvas_clear(canvas);

    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 2, 10, "MURMUR");
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str_aligned(canvas, 126, 10, AlignRight, AlignBottom,
                            app->connected ? (app->negotiated ? "linked" : "version mismatch") :
                                             "offline");

    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str_aligned(canvas, 64, 30, AlignCenter, AlignCenter,
                            murmur_phase_text(app->phase));

    canvas_set_font(canvas, FontSecondary);
    if(app->notice[0]) {
        canvas_draw_str_aligned(canvas, 64, 43, AlignCenter, AlignCenter, app->notice);
    } else if(app->last_action[0]) {
        canvas_draw_str_aligned(canvas, 64, 43, AlignCenter, AlignCenter, app->last_action);
    }

    char line[40];
    snprintf(line, sizeof(line), "OK %s", murmur_verb_name(app->map.short_press[MurmurButtonOk]));
    canvas_draw_str(canvas, 2, 56, line);
    snprintf(line, sizeof(line), "Up %s", murmur_verb_name(app->map.short_press[MurmurButtonUp]));
    canvas_draw_str(canvas, 2, 64, line);

    snprintf(line, sizeof(line), "Dn %s",
             murmur_verb_name(app->map.short_press[MurmurButtonDown]));
    canvas_draw_str_aligned(canvas, 126, 56, AlignRight, AlignTop, line);
    snprintf(line, sizeof(line), "Bk %s",
             murmur_verb_name(app->map.short_press[MurmurButtonBack]));
    canvas_draw_str_aligned(canvas, 126, 64, AlignRight, AlignTop, line);
}

static void send_line(MurmurApp* app, const char* line) {
    if(!app->connected) {
        snprintf(app->notice, sizeof(app->notice), "Not connected");
        return;
    }
    link_send(app->link, line);
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

static void handle_message(MurmurApp* app, const char* line) {
    MurmurMessage message;
    if(!murmur_parse(line, &message)) return;
    app->last_rx = furi_get_tick();

    switch(message.type) {
    case MurmurMessageHello:
        app->peer_protocol = message.protocol;
        strncpy(app->peer_app, message.app, sizeof(app->peer_app) - 1);
        app->negotiated = (message.protocol == MURMUR_PROTOCOL_VERSION);
        if(!app->negotiated) {
            snprintf(app->notice, sizeof(app->notice), "Update the app");
        }
        break;
    case MurmurMessageConfig:
        app->map = message.map;
        config_save(&app->map);
        break;
    case MurmurMessageState:
        app->phase = message.phase;
        break;
    case MurmurMessageAck:
        strncpy(app->last_action, message.app, sizeof(app->last_action) - 1);
        break;
    case MurmurMessagePing:
        // Presence only: never answer a heartbeat with another heartbeat.
        break;
    case MurmurMessageNone:
    default:
        break;
    }
}

static void service_link(MurmurApp* app) {
    char line[MURMUR_MAX_LINE];
    for(uint8_t i = 0; i < 4 && link_read_line(app->link, line, sizeof(line)); i++) {
        handle_message(app, line);
    }
    link_service(app->link);
}

static void service_timers(MurmurApp* app) {
    uint32_t now = furi_get_tick();
    if(!app->connected) {
        app->phase = MurmurPhaseOffline;
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

    bool link_ready = link_open(app->link);
    if(!link_ready) {
        snprintf(app->notice, sizeof(app->notice), "USB port busy");
    }
    app->started_at = furi_get_tick();
    app->last_hello = app->started_at - HELLO_RETRY_MS;

    while(app->running) {
        InputEvent event;
        if(furi_message_queue_get(app->queue, &event, TICK_MS) == FuriStatusOk) {
            MurmurButton button;
            if(event.key == InputKeyBack && event.type == InputTypeLong) {
                app->running = false;
            } else if(event.type == InputTypeShort || event.type == InputTypeLong) {
                if(key_to_button(event.key, &button)) {
                    handle_press(app, button, event.type == InputTypeLong);
                    notification_message(app->notifications, &sequence_single_vibro);
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
            snprintf(app->notice, sizeof(app->notice), "Cable disconnected");
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
