#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "sensor_protocol.h"

#define OPP_BTHOME_SERVICE_DATA_SIZE 10
#define OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE 20
#define OPP_BTHOME_LOCAL_NAME_SIZE 20

typedef struct {
    uint8_t packet_id;
    bool button_event;
    bool soil_available;
    opp_sensor_reading_t soil;
    bool air_available;
    int16_t air_temperature_tenths_celsius;
    uint16_t air_humidity_hundredths_percent;
} opp_bthome_sample_t;

size_t opp_bthome_encode_service_data(const opp_sensor_reading_t *reading,
                                      uint8_t output[OPP_BTHOME_SERVICE_DATA_SIZE]);

size_t opp_bthome_encode_v2_service_data(
    const opp_bthome_sample_t *sample,
    uint8_t output[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE]);

bool opp_bthome_format_local_name(uint64_t device_id,
                                  char output[OPP_BTHOME_LOCAL_NAME_SIZE]);
