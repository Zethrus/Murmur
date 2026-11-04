#include "ble.h"

#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_bt.h>

#include <bt/bt_service/bt.h>
#include <profiles/serial_profile.h>
#include <services/serial_service.h>

#include <string.h>

struct BleLink {
    Bt* bt;
    FuriHalBleProfileBase* profile;
    BleRxHandler rx;
    volatile bool connected;
};

static struct BleLink* ble;

static uint16_t ble_serial_event(SerialServiceEvent event, void* context) {
    struct BleLink* self = context;
    if(event.event != SerialServiceEventTypeDataReceived) {
        return 0;
    }
    if(self->rx && event.data.size) {
        self->rx(event.data.buffer, event.data.size);
    }
    return event.data.size; /* consumed in full: we copied it out */
}

static void ble_status_changed(BtStatus status, void* context) {
    struct BleLink* self = context;
    self->connected = (status == BtStatusConnected);
}

bool ble_start(BleRxHandler rx) {
    if(ble) return true;
    struct BleLink* self = malloc(sizeof(struct BleLink));
    if(!self) return false;
    memset(self, 0, sizeof(*self));
    self->rx = rx;

    self->bt = furi_record_open(RECORD_BT);
    bt_set_status_changed_callback(self->bt, ble_status_changed, self);

    self->profile = bt_profile_start(self->bt, ble_profile_serial, NULL);
    if(!self->profile) {
        bt_set_status_changed_callback(self->bt, NULL, NULL);
        furi_record_close(RECORD_BT);
        free(self);
        return false;
    }

    ble_profile_serial_set_event_callback(
        self->profile, BLE_SVC_SERIAL_DATA_LEN_MAX, ble_serial_event, self);
    /* The RPC transport is not using this profile; we own the stream. */
    ble_profile_serial_set_rpc_active(self->profile, false);
    furi_hal_bt_start_advertising();

    ble = self;
    return true;
}

void ble_stop(void) {
    if(!ble) return;
    ble_profile_serial_set_event_callback(ble->profile, 0, NULL, NULL);
    bt_profile_restore_default(ble->bt);
    bt_set_status_changed_callback(ble->bt, NULL, NULL);
    furi_record_close(RECORD_BT);
    free(ble);
    ble = NULL;
}

bool ble_connected(void) {
    return ble && ble->connected;
}

bool ble_send(const uint8_t* data, size_t size) {
    if(!ble || !ble->connected || !size) return false;
    /* The profile takes a mutable buffer and copies at most one packet. */
    return ble_profile_serial_tx(ble->profile, (uint8_t*)data, (uint16_t)size);
}
