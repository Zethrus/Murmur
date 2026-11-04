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
    FuriMutex* state_mutex;
    BleRxHandler rx;
    volatile bool connected;
    volatile bool ready;
    volatile uint32_t received_bytes;
    volatile uint32_t session_id;
    volatile uint32_t in_flight;
    uint32_t callback_generation;
};

static struct BleLink* ble;
static uint32_t ble_session_counter;
static uint32_t ble_callback_counter;

static struct BleLink* ble_acquire_current(void) {
    struct BleLink* self;
    FURI_CRITICAL_ENTER();
    self = ble;
    if(self) self->in_flight++;
    FURI_CRITICAL_EXIT();
    return self;
}

static struct BleLink* ble_detach_current(void) {
    struct BleLink* self;
    FURI_CRITICAL_ENTER();
    self = ble;
    if(self) {
        ble = NULL;
    }
    FURI_CRITICAL_EXIT();
    if(self) {
        furi_mutex_acquire(self->state_mutex, FuriWaitForever);
        self->ready = false;
        self->connected = false;
        furi_mutex_release(self->state_mutex);
    }
    return self;
}

static struct BleLink* ble_acquire_callback(void* context) {
    const uint32_t generation = (uint32_t)(uintptr_t)context;
    if(!generation) return NULL;

    struct BleLink* self;
    FURI_CRITICAL_ENTER();
    self = ble;
    if(!self || self->callback_generation != generation) {
        self = NULL;
    } else {
        self->in_flight++;
    }
    FURI_CRITICAL_EXIT();
    return self;
}

static void ble_release(struct BleLink* self) {
    FURI_CRITICAL_ENTER();
    self->in_flight--;
    FURI_CRITICAL_EXIT();
}

static void* ble_callback_context(const struct BleLink* self) {
    return (void*)(uintptr_t)self->callback_generation;
}

static uint16_t ble_serial_event(SerialServiceEvent event, void* context) {
    struct BleLink* self = ble_acquire_callback(context);
    if(!self) return 0;

    uint16_t consumed = 0;
    BleRxHandler rx = NULL;
    furi_mutex_acquire(self->state_mutex, FuriWaitForever);
    if(!self->ready || !self->connected) {
        furi_mutex_release(self->state_mutex);
        ble_release(self);
        return 0;
    }
    if(event.event != SerialServiceEventTypeDataReceived) {
        furi_mutex_release(self->state_mutex);
        ble_release(self);
        return 0;
    }
    if(!event.data.buffer || !event.data.size) {
        furi_mutex_release(self->state_mutex);
        ble_release(self);
        return 0;
    }
    self->received_bytes += event.data.size;
    rx = self->rx;
    consumed = event.data.size;
    furi_mutex_release(self->state_mutex);
    if(rx) rx(event.data.buffer, event.data.size);
    ble_release(self);
    return consumed; /* consumed in full: we copied it out */
}

static void ble_status_changed(BtStatus status, void* context) {
    struct BleLink* self = ble_acquire_callback(context);
    if(!self) return;
    const bool connected = (status == BtStatusConnected);
    furi_mutex_acquire(self->state_mutex, FuriWaitForever);
    if(connected && self->ready && self->profile) {
        /* GAP connect assigns the stream to firmware RPC before publishing status. */
        ble_profile_serial_set_event_callback(
            self->profile,
            BLE_SVC_SERIAL_DATA_LEN_MAX,
            ble_serial_event,
            ble_callback_context(self));
        ble_profile_serial_set_rpc_active(self->profile, false);
    }
    if(connected && !self->connected) {
        self->received_bytes = 0;
        ble_session_counter++;
        if(!ble_session_counter) ble_session_counter++;
        self->session_id = ble_session_counter;
    }
    self->connected = connected;
    if(!connected) self->received_bytes = 0;
    furi_mutex_release(self->state_mutex);
    ble_release(self);
}

bool ble_start(BleRxHandler rx) {
    if(ble) return true;
    struct BleLink* self = malloc(sizeof(struct BleLink));
    if(!self) return false;
    memset(self, 0, sizeof(*self));
    self->state_mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    if(!self->state_mutex) {
        free(self);
        return false;
    }
    self->rx = rx;
    ble_callback_counter++;
    if(!ble_callback_counter) ble_callback_counter++;
    self->callback_generation = ble_callback_counter;

    self->bt = furi_record_open(RECORD_BT);
    FURI_CRITICAL_ENTER();
    ble = self;
    FURI_CRITICAL_EXIT();
    bt_set_status_changed_callback(self->bt, ble_status_changed, ble_callback_context(self));

    self->profile = bt_profile_start(self->bt, ble_profile_serial, NULL);
    if(!self->profile) {
        ble_detach_current();
        bt_set_status_changed_callback(self->bt, NULL, NULL);
        while(self->in_flight) furi_delay_tick(1);
        furi_record_close(RECORD_BT);
        furi_mutex_free(self->state_mutex);
        free(self);
        return false;
    }

    ble_profile_serial_set_event_callback(
        self->profile,
        BLE_SVC_SERIAL_DATA_LEN_MAX,
        ble_serial_event,
        ble_callback_context(self));
    /* The RPC transport is not using this profile; we own the stream. */
    ble_profile_serial_set_rpc_active(self->profile, false);
    self->ready = true;
    furi_hal_bt_start_advertising();

    return true;
}

void ble_stop(void) {
    struct BleLink* self = ble_detach_current();
    if(!self) return;
    if(self->profile) {
        ble_profile_serial_set_event_callback(self->profile, 0, NULL, NULL);
    }
    if(self->bt) {
        bt_set_status_changed_callback(self->bt, NULL, NULL);
    }
    while(self->in_flight) furi_delay_tick(1);
    if(self->bt) {
        self->profile = NULL;
        bt_profile_restore_default(self->bt);
        furi_record_close(RECORD_BT);
        self->bt = NULL;
    }
    furi_mutex_free(self->state_mutex);
    free(self);
}

bool ble_connected(void) {
    struct BleLink* self = ble_acquire_current();
    bool connected = false;
    if(self) {
        furi_mutex_acquire(self->state_mutex, FuriWaitForever);
        connected = self->ready && self->connected;
        furi_mutex_release(self->state_mutex);
        ble_release(self);
    }
    return connected;
}

bool ble_advertising(void) {
    struct BleLink* self = ble_acquire_current();
    bool advertising = false;
    if(self) {
        furi_mutex_acquire(self->state_mutex, FuriWaitForever);
        advertising = self->ready && !self->connected;
        furi_mutex_release(self->state_mutex);
        ble_release(self);
    }
    return advertising;
}

uint32_t ble_received_bytes(void) {
    struct BleLink* self = ble_acquire_current();
    uint32_t received_bytes = 0;
    if(self) {
        furi_mutex_acquire(self->state_mutex, FuriWaitForever);
        if(self->ready) received_bytes = self->received_bytes;
        furi_mutex_release(self->state_mutex);
        ble_release(self);
    }
    return received_bytes;
}

uint32_t ble_session_id(void) {
    struct BleLink* self = ble_acquire_current();
    uint32_t session_id = 0;
    if(self) {
        furi_mutex_acquire(self->state_mutex, FuriWaitForever);
        if(self->ready) session_id = self->session_id;
        furi_mutex_release(self->state_mutex);
        ble_release(self);
    }
    return session_id;
}

bool ble_send(const uint8_t* data, size_t size) {
    return ble_send_for_session(data, size, ble_session_id());
}

bool ble_send_for_session(const uint8_t* data, size_t size, uint32_t expected_session) {
    struct BleLink* self = ble_acquire_current();
    if(!self) return false;

    bool sent = false;
    furi_mutex_acquire(self->state_mutex, FuriWaitForever);
    if(self->ready && self->connected && self->received_bytes && self->profile && data && size &&
       expected_session && self->session_id == expected_session) {
        /* Serialize status/session mutation through the complete profile TX call. */
        sent = ble_profile_serial_tx(self->profile, (uint8_t*)data, (uint16_t)size);
    }
    furi_mutex_release(self->state_mutex);
    ble_release(self);
    return sent;
}
