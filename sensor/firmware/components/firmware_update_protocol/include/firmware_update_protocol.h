#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Version 7 on the configuration characteristic: the hub asking this sensor to
 * install an image. The image itself never crosses Bluetooth. What arrives is
 * where to fetch it, how big it is, and what it must hash to, so a sensor can
 * tell a delivered image from a damaged one without trusting the network. */
#define OPP_FIRMWARE_UPDATE_PROTOCOL_VERSION 7
#define OPP_FIRMWARE_UPDATE_PAYLOAD_SIZE 47
#define OPP_FIRMWARE_UPDATE_DIGEST_SIZE 32

typedef struct {
    uint32_t update_id;
    uint32_t size_bytes;
    uint8_t address[4];
    uint16_t port;
    uint8_t digest[OPP_FIRMWARE_UPDATE_DIGEST_SIZE];
} opp_firmware_update_command_t;

/* Recognise and read a command, or leave the payload alone. Returns false for
 * every other payload version the characteristic carries, so each one is
 * decoded by exactly one reader. */
bool opp_firmware_update_decode(const uint8_t *payload, size_t payload_size,
                                opp_firmware_update_command_t *command);

size_t opp_firmware_update_encode(const opp_firmware_update_command_t *command,
                                  uint8_t output[OPP_FIRMWARE_UPDATE_PAYLOAD_SIZE]);

/* Build the address to fetch from: no file name is delivered, so a command can
 * only ever name an image the hub is already serving by its digest. Returns the
 * number of characters written, or 0 if they would not fit. */
size_t opp_firmware_update_format_url(const opp_firmware_update_command_t *command,
                                      char *output, size_t output_size);

/* What the sensor is doing, as the hub reads it back. Appended in front of the
 * station status so that suffix keeps its place at the end. */
#define OPP_FIRMWARE_STATUS_MARKER 0xA2
#define OPP_FIRMWARE_STATUS_SIZE 4

typedef enum {
    OPP_FIRMWARE_STATE_IDLE = 0,
    OPP_FIRMWARE_STATE_COMMANDED = 1,
    OPP_FIRMWARE_STATE_DOWNLOADING = 2,
    OPP_FIRMWARE_STATE_INSTALLING = 3,
    OPP_FIRMWARE_STATE_REBOOTING = 4,
    OPP_FIRMWARE_STATE_FAILED = 5,
} opp_firmware_state_t;

typedef enum {
    OPP_FIRMWARE_FAILURE_NONE = 0,
    OPP_FIRMWARE_FAILURE_NO_NETWORK = 1,
    OPP_FIRMWARE_FAILURE_DOWNLOAD = 2,
    OPP_FIRMWARE_FAILURE_DIGEST = 3,
    OPP_FIRMWARE_FAILURE_FLASH = 4,
    OPP_FIRMWARE_FAILURE_REJECTED = 5,
    OPP_FIRMWARE_FAILURE_TOO_LARGE = 6,
    OPP_FIRMWARE_FAILURE_NO_SLOT = 7,
} opp_firmware_failure_t;

size_t opp_firmware_status_encode(opp_firmware_state_t state, uint8_t percent,
                                  opp_firmware_failure_t failure,
                                  uint8_t output[OPP_FIRMWARE_STATUS_SIZE]);
