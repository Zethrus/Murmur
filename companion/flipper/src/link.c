#include "link.h"

#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_usb.h>
#include <furi_hal_usb_cdc.h>

#include <stdio.h>
#include <string.h>

#define LINK_RING 512
#define LINK_TX_SLOTS 4

struct MurmurLink {
    FuriHalUsbInterface* previous;
    CdcCallbacks callbacks;
    volatile bool connected;
    bool configured;

    /* Receive ring: written in the USB interrupt, read by the app thread. */
    uint8_t ring[LINK_RING];
    volatile size_t head;
    volatile size_t tail;

    /* Outbound queue, one line each. */
    char pending[LINK_TX_SLOTS][MURMUR_MAX_LINE];
    uint8_t pending_head;
    uint8_t pending_tail;

    char line[MURMUR_MAX_LINE];
    size_t line_length;
};

static void link_rx_callback(void* context) {
    MurmurLink* link = context;
    uint8_t chunk[64];
    int32_t got = furi_hal_cdc_receive(0, chunk, sizeof(chunk));
    for(int32_t i = 0; i < got; i++) {
        size_t next = (link->head + 1) % LINK_RING;
        if(next == link->tail) {
            break; /* overflow: drop the rest of the burst rather than corrupt order */
        }
        link->ring[link->head] = chunk[i];
        link->head = next;
    }
}

static void link_state_callback(void* context, CdcState state) {
    MurmurLink* link = context;
    link->connected = (state == CdcStateConnected);
}

MurmurLink* link_alloc(void) {
    MurmurLink* link = malloc(sizeof(MurmurLink));
    memset(link, 0, sizeof(MurmurLink));
    link->callbacks.rx_ep_callback = link_rx_callback;
    link->callbacks.state_callback = link_state_callback;
    return link;
}

void link_free(MurmurLink* link) {
    if(!link) return;
    link_close(link);
    free(link);
}

bool link_open(MurmurLink* link) {
    if(!link || link->configured) return false;
    link->previous = furi_hal_usb_get_config();
    furi_hal_usb_unlock();
    if(!furi_hal_usb_set_config(&usb_cdc_single, NULL)) {
        return false;
    }
    furi_hal_cdc_set_callbacks(0, &link->callbacks, link);
    link->configured = true;
    link->head = link->tail = 0;
    link->pending_head = link->pending_tail = 0;
    link->line_length = 0;
    return true;
}

void link_close(MurmurLink* link) {
    if(!link || !link->configured) return;
    furi_hal_cdc_set_callbacks(0, NULL, NULL);
    furi_hal_usb_set_config(link->previous ? link->previous : &usb_cdc_dual, NULL);
    furi_hal_usb_lock();
    link->configured = false;
    link->connected = false;
}

bool link_connected(MurmurLink* link) {
    return link && link->connected;
}

void link_send(MurmurLink* link, const char* line) {
    if(!link || !line) return;
    uint8_t next = (link->pending_head + 1) % LINK_TX_SLOTS;
    if(next == link->pending_tail) {
        return; /* full: the host is not draining, drop the newest line */
    }
    snprintf(link->pending[link->pending_head], MURMUR_MAX_LINE, "%s\n", line);
    link->pending_head = next;
}

void link_service(MurmurLink* link) {
    if(!link || !link->configured) return;
    if(link->pending_tail == link->pending_head) return;
    if(!link->connected) return;
    const char* line = link->pending[link->pending_tail];
    furi_hal_cdc_send(0, (uint8_t*)line, (uint16_t)strlen(line));
    link->pending_tail = (link->pending_tail + 1) % LINK_TX_SLOTS;
}

bool link_read_line(MurmurLink* link, char* out, size_t cap) {
    if(!link) return false;
    while(link->tail != link->head) {
        char byte = (char)link->ring[link->tail];
        link->tail = (link->tail + 1) % LINK_RING;
        if(byte == '\n' || byte == '\r') {
            if(link->line_length == 0) continue;
            link->line[link->line_length] = '\0';
            strncpy(out, link->line, cap - 1);
            out[cap - 1] = '\0';
            link->line_length = 0;
            return true;
        }
        if(link->line_length + 1 >= sizeof(link->line)) {
            link->line_length = 0; /* unterminated garbage: resynchronise */
            continue;
        }
        link->line[link->line_length++] = byte;
    }
    return false;
}
