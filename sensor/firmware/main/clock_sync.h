#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    bool time_valid;
    bool sync_in_progress;
    bool sync_due;
    bool rtc_retained;
    bool adjustment_known;
    int64_t unix_time_s;
    int64_t last_sync_unix_s;
    int64_t last_adjustment_ms;
    uint32_t sync_failures;
} opp_clock_status_t;

esp_err_t opp_clock_sync_init(void);
void opp_clock_sync_set_network_available(bool available);
esp_err_t opp_clock_sync_now(void);
bool opp_clock_sync_needed(void);
bool opp_clock_capture_timestamp(int64_t *unix_time_ms);
void opp_clock_get_status(opp_clock_status_t *status);