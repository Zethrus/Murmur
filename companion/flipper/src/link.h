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
void link_close(MurmurLink* link);
bool link_connected(MurmurLink* link);

/** Queue one line for the host. Silently drops when the queue is full. */
void link_send(MurmurLink* link, const char* line);
/** Flush one queued line if the port is free. Call regularly from the app loop. */
void link_service(MurmurLink* link);
/** Pull one complete line, if one has arrived. */
bool link_read_line(MurmurLink* link, char* out, size_t cap);
