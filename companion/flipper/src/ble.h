#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/** Called from the BLE service thread with one received chunk. */
typedef void (*BleRxHandler)(const uint8_t* data, size_t size);

/**
 * Start the BLE serial profile and advertise it.
 *
 * Bonding is the firmware's: the Flipper shows the PIN, the host enters it once and
 * keeps the key. Returns false when the radio will not start.
 */
bool ble_start(BleRxHandler rx);

/** Stop the profile and hand the radio back to the firmware. */
void ble_stop(void);

/** True while a central is connected. */
bool ble_connected(void);

/** Queue bytes for the central. Returns false when nothing is connected. */
bool ble_send(const uint8_t* data, size_t size);
