#include "link.h"

#include "ble.h"

#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_usb.h>
#include <furi_hal_usb_cdc.h>

#include <stdio.h>
#include <string.h>

#define LINK_RING 512
#define LINK_TX_SLOTS 4

typedef struct {
    uint8_t buffer[LINK_RING];
    volatile size_t head;
    volatile size_t tail;
    char line[MURMUR_MAX_LINE];
    size_t line_length;
} LinkRing;

typedef struct {
    char line[MURMUR_MAX_LINE];
    MurmurTransport via;
} PendingLine;

struct MurmurLink {
    FuriHalUsbInterface* previous;
    CdcCallbacks callbacks;
    bool usb_configured;
    volatile bool usb_connected;
    bool ble_started;

    /* One ring per transport: a line is always answered where it arrived. */
    LinkRing usb;
    LinkRing ble;
    MurmurTransport source;

    PendingLine pending[LINK_TX_SLOTS];
    uint8_t pending_head;
    uint8_t pending_tail;

};

static void ring_push(LinkRing* ring, const uint8_t* data, size_t size) {
    for(size_t i = 0; i < size; i++) {
        size_t next = (ring->head + 1) % LINK_RING;
        if(next == ring->tail) {
            return; /* overflow: drop the rest of the burst rather than corrupt order */
        }
        ring->buffer[ring->head] = data[i];
        ring->head = next;
    }
}

static void link_usb_rx(void* context) {
    MurmurLink* link = context;
    uint8_t chunk[64];
    int32_t got = furi_hal_cdc_receive(0, chunk, sizeof(chunk));
    if(got > 0) ring_push(&link->usb, chunk, (size_t)got);
}

static void link_usb_state(void* context, CdcState state) {
    MurmurLink* link = context;
    link->usb_connected = (state == CdcStateConnected);
}

MurmurLink* link_alloc(void) {
    MurmurLink* link = malloc(sizeof(MurmurLink));
    memset(link, 0, sizeof(MurmurLink));
    link->callbacks.rx_ep_callback = link_usb_rx;
    link->callbacks.state_callback = link_usb_state;
    return link;
}

void link_free(MurmurLink* link) {
    if(!link) return;
    link_close(link);
    free(link);
}

bool link_open(MurmurLink* link) {
    if(!link || link->usb_configured) return false;
    link->previous = furi_hal_usb_get_config();
    furi_hal_usb_unlock();
    if(!furi_hal_usb_set_config(&usb_cdc_single, NULL)) {
        return false;
    }
    furi_hal_cdc_set_callbacks(0, &link->callbacks, link);
    link->usb_configured = true;
    link->pending_head = link->pending_tail = 0;
    link->usb.line_length = 0;
    link->ble.line_length = 0;
    return true;
}

/* The BLE callback needs the instance, which the profile only knows through link_open_ble. */
static MurmurLink* ble_owner;

static void link_ble_incoming(const uint8_t* data, size_t size) {
    if(ble_owner) ring_push(&ble_owner->ble, data, size);
}

bool link_open_ble(MurmurLink* link) {
    if(!link || link->ble_started) return false;
    ble_owner = link;
    if(!ble_start(link_ble_incoming)) {
        ble_owner = NULL;
        return false;
    }
    link->ble_started = true;
    return true;
}

void link_close(MurmurLink* link) {
    if(!link) return;
    if(link->ble_started) {
        ble_stop();
        link->ble_started = false;
        ble_owner = NULL;
    }
    if(!link->usb_configured) return;
    furi_hal_cdc_set_callbacks(0, NULL, NULL);
    furi_hal_usb_set_config(link->previous ? link->previous : &usb_cdc_dual, NULL);
    furi_hal_usb_lock();
    link->usb_configured = false;
    link->usb_connected = false;
}

MurmurTransport link_transport(MurmurLink* link) {
    return link ? link->source : MurmurTransportNone;
}

bool link_connected(MurmurLink* link) {
    if(!link) return false;
    return link->usb_connected || ble_connected();
}

/** Unsolicited messages use the active transport; replies retain their source. */
static MurmurTransport outbound(MurmurLink* link) {
    if(link->source == MurmurTransportUsb && link->usb_connected) return link->source;
    if(link->source == MurmurTransportBle && ble_connected()) return link->source;
    if(link->usb_connected) return MurmurTransportUsb;
    if(ble_connected()) return MurmurTransportBle;
    return MurmurTransportNone;
}

static bool queue_line(MurmurLink* link, const char* line, MurmurTransport via) {
    if(!link || !line) return false;
    uint8_t next = (link->pending_head + 1) % LINK_TX_SLOTS;
    if(next == link->pending_tail) {
        return false; /* full: the host is not draining */
    }
    PendingLine* pending = &link->pending[link->pending_head];
    snprintf(pending->line, MURMUR_MAX_LINE, "%s\n", line);
    pending->via = via;
    link->pending_head = next;
    return true;
}

bool link_send(MurmurLink* link, const char* line) {
    if(!link) return false;
    MurmurTransport via = outbound(link);
    return via != MurmurTransportNone && queue_line(link, line, via);
}

void link_send_reply(MurmurLink* link, const char* line) {
    if(link) queue_line(link, line, link->source);
}

void link_service(MurmurLink* link) {
    if(!link) return;
    if(link->pending_tail == link->pending_head) return;

    MurmurTransport via = link->pending[link->pending_tail].via;
    if(via == MurmurTransportNone ||
       (via == MurmurTransportUsb && !link->usb_connected) ||
       (via == MurmurTransportBle && !ble_connected())) {
        link->pending_tail = (link->pending_tail + 1) % LINK_TX_SLOTS;
        return; /* a vanished peer must not block replies to the other transport */
    }

    const char* line = link->pending[link->pending_tail].line;
    size_t length = strlen(line);
    bool sent;
    if(via == MurmurTransportUsb) {
        furi_hal_cdc_send(0, (uint8_t*)line, (uint16_t)length);
        sent = true;
    } else {
        sent = ble_send((const uint8_t*)line, length);
    }
    if(!sent) return; /* keep it queued; the transport is not ready */
    link->pending_tail = (link->pending_tail + 1) % LINK_TX_SLOTS;
}

static bool drain_ring(MurmurLink* link, LinkRing* ring, MurmurTransport source,
                       char* out, size_t cap) {
    while(ring->tail != ring->head) {
        char byte = (char)ring->buffer[ring->tail];
        ring->tail = (ring->tail + 1) % LINK_RING;
        if(byte == '\n' || byte == '\r') {
            if(ring->line_length == 0) continue;
            ring->line[ring->line_length] = '\0';
            strncpy(out, ring->line, cap - 1);
            out[cap - 1] = '\0';
            ring->line_length = 0;
            link->source = source;
            return true;
        }
        if(ring->line_length + 1 >= sizeof(ring->line)) {
            ring->line_length = 0; /* unterminated garbage: resynchronise */
            continue;
        }
        ring->line[ring->line_length++] = byte;
    }
    return false;
}

bool link_read_line(MurmurLink* link, char* out, size_t cap) {
    if(!link) return false;
    if(drain_ring(link, &link->usb, MurmurTransportUsb, out, cap)) return true;
    return drain_ring(link, &link->ble, MurmurTransportBle, out, cap);
}
