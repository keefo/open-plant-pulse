#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "sht45_types.h"

#define OPP_SHT45_RESPONSE_SIZE 6

uint8_t opp_sht45_crc8(const uint8_t *data, size_t length);

bool opp_sht45_decode_response(const uint8_t response[OPP_SHT45_RESPONSE_SIZE],
                               opp_sht45_sample_t *sample);