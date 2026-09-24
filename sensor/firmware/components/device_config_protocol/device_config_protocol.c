#include "device_config_protocol.h"

#include <string.h>

#define FIXED_PAYLOAD_SIZE 11

static bool is_valid_utf8(const uint8_t *text, size_t length)
{
    size_t offset = 0;
    while (offset < length) {
        const uint8_t first = text[offset++];
        if (first == 0 || first < 0x20 || first == 0x7f) {
            return false;
        }
        if (first < 0x80) {
            continue;
        }

        size_t continuation_count;
        uint32_t codepoint;
        uint32_t minimum;
        if ((first & 0xe0) == 0xc0) {
            continuation_count = 1;
            codepoint = first & 0x1f;
            minimum = 0x80;
        } else if ((first & 0xf0) == 0xe0) {
            continuation_count = 2;
            codepoint = first & 0x0f;
            minimum = 0x800;
        } else if ((first & 0xf8) == 0xf0) {
            continuation_count = 3;
            codepoint = first & 0x07;
            minimum = 0x10000;
        } else {
            return false;
        }
        if (offset + continuation_count > length) {
            return false;
        }
        for (size_t index = 0; index < continuation_count; index++) {
            const uint8_t continuation = text[offset++];
            if ((continuation & 0xc0) != 0x80) {
                return false;
            }
            codepoint = (codepoint << 6U) | (continuation & 0x3f);
        }
        if (codepoint < minimum || codepoint > 0x10ffff ||
            (codepoint >= 0xd800 && codepoint <= 0xdfff)) {
            return false;
        }
    }
    return true;
}

static bool text_length(const char *text, size_t *length)
{
    *length = 0;
    while (*length <= OPP_DEVICE_CONFIG_TEXT_MAX_BYTES && text[*length] != '\0') {
        (*length)++;
    }
    return *length <= OPP_DEVICE_CONFIG_TEXT_MAX_BYTES &&
           is_valid_utf8((const uint8_t *)text, *length);
}

size_t opp_device_config_encode(
    const opp_device_config_t *config,
    uint8_t output[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE])
{
    if (config == NULL || output == NULL || config->revision == 0 ||
        config->reporting_interval_seconds < OPP_DEVICE_CONFIG_MIN_INTERVAL_SECONDS ||
        config->reporting_interval_seconds > OPP_DEVICE_CONFIG_MAX_INTERVAL_SECONDS) {
        return 0;
    }

    size_t plant_name_length;
    size_t room_length;
    if (!text_length(config->plant_name, &plant_name_length) || plant_name_length == 0 ||
        !text_length(config->room, &room_length)) {
        return 0;
    }

    output[0] = OPP_DEVICE_CONFIG_PROTOCOL_VERSION;
    output[1] = (uint8_t)config->revision;
    output[2] = (uint8_t)(config->revision >> 8U);
    output[3] = (uint8_t)(config->revision >> 16U);
    output[4] = (uint8_t)(config->revision >> 24U);
    output[5] = (uint8_t)config->reporting_interval_seconds;
    output[6] = (uint8_t)(config->reporting_interval_seconds >> 8U);
    output[7] = (uint8_t)(config->reporting_interval_seconds >> 16U);
    output[8] = (uint8_t)(config->reporting_interval_seconds >> 24U);
    output[9] = (uint8_t)plant_name_length;
    output[10] = (uint8_t)room_length;
    memcpy(output + FIXED_PAYLOAD_SIZE, config->plant_name, plant_name_length);
    memcpy(output + FIXED_PAYLOAD_SIZE + plant_name_length, config->room, room_length);
    return FIXED_PAYLOAD_SIZE + plant_name_length + room_length;
}

bool opp_device_config_decode(const uint8_t *payload, size_t payload_size,
                              opp_device_config_t *config)
{
    if (payload == NULL || config == NULL || payload_size < FIXED_PAYLOAD_SIZE ||
        payload_size > OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE ||
        payload[0] != OPP_DEVICE_CONFIG_PROTOCOL_VERSION) {
        return false;
    }

    const uint32_t revision = (uint32_t)payload[1] |
                              ((uint32_t)payload[2] << 8U) |
                              ((uint32_t)payload[3] << 16U) |
                              ((uint32_t)payload[4] << 24U);
    const uint32_t interval = (uint32_t)payload[5] |
                              ((uint32_t)payload[6] << 8U) |
                              ((uint32_t)payload[7] << 16U) |
                              ((uint32_t)payload[8] << 24U);
    const size_t plant_name_length = payload[9];
    const size_t room_length = payload[10];
    if (revision == 0 || interval < OPP_DEVICE_CONFIG_MIN_INTERVAL_SECONDS ||
        interval > OPP_DEVICE_CONFIG_MAX_INTERVAL_SECONDS || plant_name_length == 0 ||
        plant_name_length > OPP_DEVICE_CONFIG_TEXT_MAX_BYTES ||
        room_length > OPP_DEVICE_CONFIG_TEXT_MAX_BYTES ||
        payload_size != FIXED_PAYLOAD_SIZE + plant_name_length + room_length ||
        !is_valid_utf8(payload + FIXED_PAYLOAD_SIZE, plant_name_length) ||
        !is_valid_utf8(payload + FIXED_PAYLOAD_SIZE + plant_name_length, room_length)) {
        return false;
    }

    memset(config, 0, sizeof(*config));
    config->revision = revision;
    config->reporting_interval_seconds = interval;
    memcpy(config->plant_name, payload + FIXED_PAYLOAD_SIZE, plant_name_length);
    memcpy(config->room, payload + FIXED_PAYLOAD_SIZE + plant_name_length, room_length);
    return true;
}

bool opp_device_config_equal(const opp_device_config_t *left,
                             const opp_device_config_t *right)
{
    return left != NULL && right != NULL && left->revision == right->revision &&
           left->reporting_interval_seconds == right->reporting_interval_seconds &&
           strcmp(left->plant_name, right->plant_name) == 0 &&
           strcmp(left->room, right->room) == 0;
}