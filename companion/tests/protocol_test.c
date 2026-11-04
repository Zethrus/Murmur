#include "../flipper/src/protocol.h"
#include "../flipper/src/link.h"

#include <assert.h>
#include <string.h>

int main(void) {
    MurmurMessage message;
    assert(murmur_parse("T preview", &message));
    assert(message.type == MurmurMessageText);
    assert(strcmp(message.text, "preview") == 0);

    assert(murmur_parse("T", &message));
    assert(message.type == MurmurMessageText);
    assert(message.text[0] == '\0');

    assert(murmur_parse("T ", &message));
    assert(message.type == MurmurMessageText);
    assert(message.text[0] == '\0');

    assert(link_choose_transport(
               3000, true, true, 2500, true, true, 2990) == MurmurTransportUsb);
    assert(link_choose_transport(
               5001, true, true, 3000, true, true, 4500) == MurmurTransportBle);
    assert(link_choose_transport(
               7001, true, true, 5000, true, true, 5000) == MurmurTransportNone);
    assert(link_choose_transport(
               100, true, false, 0, true, false, 0) == MurmurTransportNone);
    /* Tick wrap must preserve a recent transport. */
    assert(link_choose_transport(
               10, true, true, UINT32_MAX - 100, false, false, 0) == MurmurTransportUsb);

    assert(!link_transport_can_enqueue(MurmurTransportBle, false));
    assert(link_transport_can_enqueue(MurmurTransportBle, true));
    assert(link_transport_can_enqueue(MurmurTransportUsb, false));
    assert(link_ble_session_matches(7, 7));
    assert(!link_ble_session_matches(7, 8));
    assert(link_pending_valid(MurmurTransportUsb, 7, 8));
    assert(link_pending_valid(MurmurTransportBle, 8, 8));
    assert(!link_pending_valid(MurmurTransportBle, 7, 8));
    return 0;
}
