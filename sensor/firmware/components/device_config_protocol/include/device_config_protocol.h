#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define OPP_DEVICE_CONFIG_PROTOCOL_VERSION 2
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