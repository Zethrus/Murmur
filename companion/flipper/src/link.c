#include "link.h"

#include "ble.h"

#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_usb.h>
#include <furi_hal_usb_cdc.h>

#include <stdio.h>
#include <string.h>

#define LINK_RING     512
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
    uint32_t ble_session;
} PendingLine;

struct MurmurLink {
    FuriHalUsbInterface* previous;
    CdcCallbacks callbacks;
    bool usb_configured;
    volatile bool usb_connected;
    bool ble_started;
    FuriMutex* ble_mutex;

    /* One ring per transport: a line is always answered where it arrived. */
    LinkRing usb;
    LinkRing ble;
    MurmurTransport source;
    volatile bool usb_received;
    bool usb_has_frame;
    uint32_t ble_session;
    volatile uint32_t ble_rx_session;
    volatile uint32_t ble_received_session;
    uint32_t ble_frame_session;
    volatile uint32_t usb_received_bytes;
    volatile uint32_t ble_received_bytes;
    uint32_t usb_last_frame;
    uint32_t ble_last_frame;

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
    if(!link) return;
    uint8_t chunk[64];
    int32_t got = furi_hal_cdc_receive(0, chunk, sizeof(chunk));
    if(got > 0) {
        link->usb_received = true;
        link->usb_received_bytes += (uint32_t)got;
        ring_push(&link->usb, chunk, (size_t)got);
    }
}

static void link_usb_state(void* context, CdcState state) {
    MurmurLink* link = context;
    if(!link) return;
    bool connected = (state == CdcStateConnected);
    if(connected && !link->usb_connected) {
        link->usb_received = false;
        link->usb_has_frame = false;
        link->usb_received_bytes = 0;
        link->usb_last_frame = 0;
    } else if(!connected) {
        link->usb_received = false;
    }
    link->usb_connected = connected;
}

MurmurLink* link_alloc(void) {
    MurmurLink* link = malloc(sizeof(MurmurLink));
    if(!link) return NULL;
    memset(link, 0, sizeof(MurmurLink));
    link->ble_mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    if(!link->ble_mutex) {
        free(link);
        return NULL;
    }
    link->callbacks.rx_ep_callback = link_usb_rx;
    link->callbacks.state_callback = link_usb_state;
    return link;
}

void link_free(MurmurLink* link) {
    if(!link) return;
    link_close(link);
    furi_mutex_free(link->ble_mutex);
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
    link->usb_received = false;
    link->usb_has_frame = false;
    link->ble_session = 0;
    link->ble_rx_session = 0;
    link->ble_received_session = 0;
    link->ble_frame_session = 0;
    link->usb_last_frame = 0;
    link->ble_last_frame = 0;
    return true;
}

/* The BLE callback needs the instance, which the profile only knows through link_open_ble. */
static MurmurLink* ble_owner;

static void purge_stale_ble_pending(MurmurLink* link) {
    uint8_t read = link->pending_tail;
    uint8_t write = link->pending_tail;
    while(read != link->pending_head) {
        PendingLine* pending = &link->pending[read];
        bool keep = link_pending_valid(pending->via, pending->ble_session, link->ble_session);
        if(keep) {
            if(write != read) link->pending[write] = *pending;
            write = (write + 1) % LINK_TX_SLOTS;
        }
        read = (read + 1) % LINK_TX_SLOTS;
    }
    link->pending_head = write;
}

static void refresh_ble_session(MurmurLink* link) {
    uint32_t session = ble_session_id();
    if(session != link->ble_session) {
        link->ble_session = session;
        purge_stale_ble_pending(link);
        if(link->source == MurmurTransportBle) link->source = MurmurTransportNone;
    }
}

static void link_ble_incoming(const uint8_t* data, size_t size) {
    MurmurLink* link = ble_owner;
    if(!link || !data || !size) return;
    if(furi_mutex_acquire(link->ble_mutex, FuriWaitForever) != FuriStatusOk) return;
    uint32_t session = ble_session_id();
    if(session != link->ble_rx_session) {
        link->ble.head = 0;
        link->ble.tail = 0;
        link->ble.line_length = 0;
        link->ble_received_bytes = 0;
        link->ble_rx_session = session;
    }
    link->ble_received_bytes += (uint32_t)size;
    ring_push(&link->ble, data, size);
    link->ble_received_session = session;
    furi_mutex_release(link->ble_mutex);
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
        link->ble_session = 0;
        link->ble_rx_session = 0;
        link->ble_received_session = 0;
        link->ble_frame_session = 0;
        link->ble_received_bytes = 0;
    }
    if(!link->usb_configured) return;
    furi_hal_cdc_set_callbacks(0, NULL, NULL);
    furi_hal_usb_set_config(link->previous ? link->previous : &usb_cdc_dual, NULL);
    furi_hal_usb_lock();
    link->usb_configured = false;
    link->usb_connected = false;
}

MurmurTransport link_transport(MurmurLink* link) {
    if(!link) return MurmurTransportNone;
    /* Read-only: this is called from the drawing thread. */
    if(link->source == MurmurTransportBle && ble_session_id() != link->ble_session) {
        return MurmurTransportNone;
    }
    return link->source;
}

uint32_t link_received_bytes(MurmurLink* link, MurmurTransport transport) {
    if(!link) return 0;
    if(transport == MurmurTransportUsb) return link->usb_received_bytes;
    if(transport == MurmurTransportBle) return link->ble_received_bytes;
    return 0;
}

bool link_connected(MurmurLink* link) {
    if(!link) return false;
    refresh_ble_session(link);
    return link->usb_connected || ble_connected();
}

/** Unsolicited messages use the active transport; replies retain their source. */
static MurmurTransport outbound(MurmurLink* link) {
    refresh_ble_session(link);
    MurmurTransport via = link_choose_transport(
        furi_get_tick(),
        link->usb_connected,
        link->usb_has_frame,
        link->usb_last_frame,
        ble_connected(),
        link->ble_frame_session == link->ble_session,
        link->ble_last_frame);
    if(via != MurmurTransportNone) return via;
    /* Nothing arrived recently. The device still has to be able to speak first — button
     * presses, battery and the heartbeat all originate here — so fall back to a live link. */
    if(link->source == MurmurTransportBle && ble_connected()) return MurmurTransportBle;
    if(link->usb_connected) return MurmurTransportUsb;
    return MurmurTransportNone;
}

static bool queue_line(MurmurLink* link, const char* line, MurmurTransport via) {
    if(!link || !line) return false;
    uint32_t current_ble_session = ble_session_id();
    bool ble_received = ble_connected() && current_ble_session == link->ble_session &&
                        link->ble_received_session == current_ble_session;
    if(!link_transport_can_enqueue(via, ble_received)) return false;
    uint8_t next = (link->pending_head + 1) % LINK_TX_SLOTS;
    if(next == link->pending_tail) {
        return false; /* full: the host is not draining */
    }
    PendingLine* pending = &link->pending[link->pending_head];
    snprintf(pending->line, MURMUR_MAX_LINE, "%s\n", line);
    pending->via = via;
    pending->ble_session = link->ble_session;
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
    refresh_ble_session(link);
    if(link->pending_tail == link->pending_head) return;

    PendingLine* pending = &link->pending[link->pending_tail];
    MurmurTransport via = pending->via;
    uint32_t current_ble_session = ble_session_id();
    if(via == MurmurTransportNone || (via == MurmurTransportUsb && !link->usb_connected) ||
       (via == MurmurTransportBle &&
        (!ble_connected() ||
         !link_pending_valid(via, pending->ble_session, current_ble_session)))) {
        link->pending_tail = (link->pending_tail + 1) % LINK_TX_SLOTS;
        return; /* a vanished peer must not block replies to the other transport */
    }

    const char* line = pending->line;
    size_t length = strlen(line);
    bool sent;
    if(via == MurmurTransportUsb) {
        furi_hal_cdc_send(0, (uint8_t*)line, (uint16_t)length);
        sent = true;
    } else {
        sent = ble_send_for_session((const uint8_t*)line, length, pending->ble_session);
    }
    if(!sent) return; /* keep it queued; the transport is not ready */
    link->pending_tail = (link->pending_tail + 1) % LINK_TX_SLOTS;
}

static bool
    drain_ring(MurmurLink* link, LinkRing* ring, MurmurTransport source, char* out, size_t cap) {
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
            uint32_t now = furi_get_tick();
            if(source == MurmurTransportUsb) {
                link->usb_has_frame = true;
                link->usb_last_frame = now;
            } else if(source == MurmurTransportBle) {
                link->ble_frame_session = link->ble_session;
                link->ble_last_frame = now;
            }
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
    refresh_ble_session(link);
    if(drain_ring(link, &link->usb, MurmurTransportUsb, out, cap)) return true;
    if(furi_mutex_acquire(link->ble_mutex, FuriWaitForever) != FuriStatusOk) return false;
    uint32_t current_ble_session = ble_session_id();
    bool received = current_ble_session == link->ble_session &&
                    link->ble_rx_session == current_ble_session &&
                    link->ble_received_session == current_ble_session;
    bool ready = received && drain_ring(link, &link->ble, MurmurTransportBle, out, cap);
    furi_mutex_release(link->ble_mutex);
    return ready;
}
