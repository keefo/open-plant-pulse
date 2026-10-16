#pragma once

#include <stdint.h>

#include "esp_err.h"

/* Report IDs for contract v3: start at 1, only ever increase, never repeat,
 * across resets, deep sleep and firmware updates. Gaps are allowed. NVS must be
 * initialised first. */
esp_err_t opp_report_id_init(void);
/* The next unused ID, or 0 if it could not be reserved durably. */
uint32_t opp_report_id_next(void);
