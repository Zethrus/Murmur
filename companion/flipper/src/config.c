#include "config.h"

#include <furi.h>
#include <storage/storage.h>

#include <string.h>

#define CONFIG_DIR "/ext/apps_data/murmur_companion"
#define CONFIG_FILE CONFIG_DIR "/config.txt"

void config_load(MurmurMap* map) {
    murmur_map_default(map);

    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* file = storage_file_alloc(storage);
    char buffer[MURMUR_MAX_LINE + 1] = {0};
    if(storage_file_open(file, CONFIG_FILE, FSAM_READ, FSOM_OPEN_EXISTING)) {
        size_t read = storage_file_read(file, buffer, MURMUR_MAX_LINE);
        buffer[read] = '\0';
        storage_file_close(file);
        MurmurMessage message;
        if(read > 0 && murmur_parse(buffer, &message) && message.type == MurmurMessageConfig) {
            *map = message.map;
        }
    }
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
}

void config_save(const MurmurMap* map) {
    char line[MURMUR_MAX_LINE + 1];
    size_t length = murmur_build_cfg(map, line, sizeof(line));
    if(length == 0) return;

    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_common_mkdir(storage, CONFIG_DIR);
    File* file = storage_file_alloc(storage);
    if(storage_file_open(file, CONFIG_FILE, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        storage_file_write(file, line, strlen(line));
        storage_file_close(file);
    }
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
}
