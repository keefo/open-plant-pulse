#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define OPP_DEVICE_CONFIG_PROTOCOL_VERSION 2
/* Written by the hub that owns this sensor to give it up. The sensor forgets
 * its configuration, its network and its bond, and returns to announcing itself
 * for adoption. Distinguished from the other payloads by its version byte, as
 * they all are, and deliberately tiny so it cannot be confused with one. */
#define OPP_DEVICE_RELEASE_PROTOCOL_VERSION 5
#define OPP_DEVICE_RELEASE_PAYLOAD_SIZE 2
#define OPP_DEVICE_RELEASE_CONFIRM 0x5A

bool opp_device_release_matches(const uint8_t *payload, size_t payload_size);
#define OPP_DEVICE_CONFIG_TEXT_MAX_BYTES 80
#define OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE 171
#define OPP_DEVICE_CONFIG_MIN_INTERVAL_SECONDS 1
#define OPP_DEVICE_CONFIG_MAX_INTERVAL_SECONDS 86400

typedef struct {
    uint32_t revision;
    uint32_t reporting_interval_seconds;
    char plant_name[OPP_DEVICE_CONFIG_TEXT_MAX_BYTES + 1];
    char room[OPP_DEVICE_CONFIG_TEXT_MAX_BYTES + 1];
} opp_device_config_t;

size_t opp_device_config_encode(
    const opp_device_config_t *config,
    uint8_t output[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE]);

bool opp_device_config_decode(const uint8_t *payload, size_t payload_size,
                              opp_device_config_t *config);

bool opp_device_config_equal(const opp_device_config_t *left,
                             const opp_device_config_t *right);