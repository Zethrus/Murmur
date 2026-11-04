#include "../flipper/src/protocol.h"

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
    return 0;
}
