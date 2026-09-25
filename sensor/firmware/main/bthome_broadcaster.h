#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t opp_bthome_broadcast(const char *local_name,
                               const uint8_t *service_data,
                               size_t service_data_size,
                               uint32_t window_ms,
                               uint32_t interval_ms);
/* When a hub last connected, in milliseconds since boot, or 0 if never.
 *
 * A sensor cannot know that settings are about to change, but it can know that
 * somebody is talking to it right now, which is when changes arrive in bursts. */
int64_t opp_bthome_last_connection_ms(void);
