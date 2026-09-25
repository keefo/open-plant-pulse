#include "firmware_update_protocol.h"

#include <stdio.h>
#include <string.h>

bool opp_firmware_update_decode(const uint8_t *payload, size_t payload_size,
                                opp_firmware_update_command_t *command)
{
    if (payload == NULL || command == NULL) {
        return false;
    }
    if (payload_size != OPP_FIRMWARE_UPDATE_PAYLOAD_SIZE) {
        return false;
    }
    if (payload[0] != OPP_FIRMWARE_UPDATE_PROTOCOL_VERSION) {
        return false;
    }
    opp_firmware_update_command_t decoded = {0};
    decoded.update_id = (uint32_t)payload[1] | ((uint32_t)payload[2] << 8U) |
                        ((uint32_t)payload[3] << 16U) | ((uint32_t)payload[4] << 24U);
    decoded.size_bytes = (uint32_t)payload[5] | ((uint32_t)payload[6] << 8U) |
                         ((uint32_t)payload[7] << 16U) | ((uint32_t)payload[8] << 24U);
    memcpy(decoded.address, &payload[9], sizeof(decoded.address));
    decoded.port = (uint16_t)((uint16_t)payload[13] | ((uint16_t)payload[14] << 8U));
    memcpy(decoded.digest, &payload[15], sizeof(decoded.digest));
    /* A command that cannot be acted on is not a command. Refusing it here
     * keeps every caller from having to ask the same questions again. */
    if (decoded.update_id == 0 || decoded.size_bytes == 0 || decoded.port == 0) {
        return false;
    }
    *command = decoded;
    return true;
}

size_t opp_firmware_update_encode(const opp_firmware_update_command_t *command,
                                  uint8_t output[OPP_FIRMWARE_UPDATE_PAYLOAD_SIZE])
{
    if (command == NULL || output == NULL) {
        return 0;
    }
    if (command->update_id == 0 || command->size_bytes == 0 || command->port == 0) {
        return 0;
    }
    output[0] = OPP_FIRMWARE_UPDATE_PROTOCOL_VERSION;
    output[1] = (uint8_t)command->update_id;
    output[2] = (uint8_t)(command->update_id >> 8U);
    output[3] = (uint8_t)(command->update_id >> 16U);
    output[4] = (uint8_t)(command->update_id >> 24U);
    output[5] = (uint8_t)command->size_bytes;
    output[6] = (uint8_t)(command->size_bytes >> 8U);
    output[7] = (uint8_t)(command->size_bytes >> 16U);
    output[8] = (uint8_t)(command->size_bytes >> 24U);
    memcpy(&output[9], command->address, sizeof(command->address));
    output[13] = (uint8_t)command->port;
    output[14] = (uint8_t)(command->port >> 8U);
    memcpy(&output[15], command->digest, sizeof(command->digest));
    return OPP_FIRMWARE_UPDATE_PAYLOAD_SIZE;
}

size_t opp_firmware_update_format_url(const opp_firmware_update_command_t *command,
                                      char *output, size_t output_size)
{
    if (command == NULL || output == NULL) {
        return 0;
    }
    char digest[OPP_FIRMWARE_UPDATE_DIGEST_SIZE * 2 + 1];
    for (size_t index = 0; index < OPP_FIRMWARE_UPDATE_DIGEST_SIZE; ++index) {
        snprintf(&digest[index * 2], 3, "%02x", command->digest[index]);
    }
    const int written = snprintf(output, output_size, "http://%u.%u.%u.%u:%u/firmware/%s.bin",
                                 command->address[0], command->address[1], command->address[2],
                                 command->address[3], command->port, digest);
    if (written <= 0 || (size_t)written >= output_size) {
        return 0;
    }
    return (size_t)written;
}

size_t opp_firmware_status_encode(opp_firmware_state_t state, uint8_t percent,
                                  opp_firmware_failure_t failure,
                                  uint8_t output[OPP_FIRMWARE_STATUS_SIZE])
{
    if (output == NULL) {
        return 0;
    }
    output[0] = OPP_FIRMWARE_STATUS_MARKER;
    output[1] = (uint8_t)state;
    output[2] = percent > 100U ? 100U : percent;
    output[3] = (uint8_t)failure;
    return OPP_FIRMWARE_STATUS_SIZE;
}
