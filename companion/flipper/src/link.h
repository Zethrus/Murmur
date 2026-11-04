#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "protocol.h"

typedef struct MurmurLink MurmurLink;

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

/** Queue one line for the host. Silently drops when the queue is full. */
void link_send(MurmurLink* link, const char* line);
/** Queue a reply on the transport that supplied the most recent line. */
void link_send_reply(MurmurLink* link, const char* line);
/** Flush one queued line if a transport is free. Call regularly from the app loop. */
void link_service(MurmurLink* link);
/** Pull one complete line, if one has arrived. */
bool link_read_line(MurmurLink* link, char* out, size_t cap);
