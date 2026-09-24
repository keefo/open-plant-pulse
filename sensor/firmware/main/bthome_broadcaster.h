#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t opp_bthome_broadcast(const char *local_name,
                               const uint8_t *service_data,
                               size_t service_data_size,
                               uint32_t window_ms,
                               uint32_t interval_ms);