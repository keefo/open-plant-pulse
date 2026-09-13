#pragma once

#include <stddef.h>
#include <stdint.h>

#include "sensor_protocol.h"

#define OPP_BTHOME_SERVICE_DATA_SIZE 10

size_t opp_bthome_encode_service_data(const opp_sensor_reading_t *reading,
                                      uint8_t output[OPP_BTHOME_SERVICE_DATA_SIZE]);