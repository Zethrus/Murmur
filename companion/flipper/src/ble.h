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

/** True while the serial profile is active and waiting for a central. */
bool ble_advertising(void);

/** Bytes received by the current BLE connection. */
uint32_t ble_received_bytes(void);

/** RAM-only generation of the current or most recent BLE connection. */
uint32_t ble_session_id(void);

/** Queue bytes after the current central has sent data. */
bool ble_send(const uint8_t* data, size_t size);

/** Queue bytes only if the current connection matches expected_session. */
bool ble_send_for_session(const uint8_t* data, size_t size, uint32_t expected_session);
