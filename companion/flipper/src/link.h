#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "protocol.h"

typedef struct MurmurLink MurmurLink;

#define LINK_TRANSPORT_RECENCY_MS 2000U

/**
 * Choose the transport for an unsolicited line.
 *
 * USB has priority while it has carried a complete frame recently. Otherwise
 * BLE is selected when it has carried a recent frame. Unsigned subtraction
 * keeps the comparison valid when the tick counter wraps.
 */
static inline MurmurTransport link_choose_transport(
    uint32_t now,
    bool usb_connected,
    bool usb_has_frame,
    uint32_t usb_last_frame,
    bool ble_is_connected,
    bool ble_has_frame,
    uint32_t ble_last_frame) {
    bool usb_recent =
        usb_connected && usb_has_frame && (uint32_t)(now - usb_last_frame) <= LINK_TRANSPORT_RECENCY_MS;
    bool ble_recent =
        ble_is_connected && ble_has_frame && (uint32_t)(now - ble_last_frame) <= LINK_TRANSPORT_RECENCY_MS;
    if(usb_recent) return MurmurTransportUsb;
    if(ble_recent) return MurmurTransportBle;
    return MurmurTransportNone;
}

/** BLE must receive a chunk in the current session before anything is queued. */
static inline bool link_transport_can_enqueue(MurmurTransport via, bool ble_received) {
    return via != MurmurTransportBle || ble_received;
}

/** Pending BLE data is valid only for the connection that queued it. */
static inline bool link_ble_session_matches(uint32_t queued_session, uint32_t current_session) {
    return queued_session == current_session;
}

/** USB entries survive BLE reconnects; BLE entries belong to one session. */
static inline bool link_pending_valid(
    MurmurTransport via, uint32_t queued_session, uint32_t current_ble_session) {
    return via != MurmurTransportBle ||
           link_ble_session_matches(queued_session, current_ble_session);
}

/** Claim the USB serial port. Returns false when another app holds it. */
MurmurLink* link_alloc(void);
void link_free(MurmurLink* link);
bool link_open(MurmurLink* link);

/** Start the BLE transport alongside USB. Safe to call when the radio is off. */
bool link_open_ble(MurmurLink* link);

void link_close(MurmurLink* link);
/** True when either transport has a host attached. */
bool link_connected(MurmurLink* link);
/** The transport the host last used, which is where replies go. */
MurmurTransport link_transport(MurmurLink* link);
/** Bytes received during the active transport session. Counters are never persisted. */
uint32_t link_received_bytes(MurmurLink* link, MurmurTransport transport);

/** Queue one line for the host. Returns false when the queue is full. */
bool link_send(MurmurLink* link, const char* line);
/** Queue a reply on the transport that supplied the most recent line. */
void link_send_reply(MurmurLink* link, const char* line);
/** Flush one queued line if a transport is free. Call regularly from the app loop. */
void link_service(MurmurLink* link);
/** Pull one complete line, if one has arrived. */
bool link_read_line(MurmurLink* link, char* out, size_t cap);
