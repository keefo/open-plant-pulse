#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "sht45.h"

typedef struct {
    opp_sht45_sample_t values;
    uint64_t sequence;
    int64_t monotonic_ms;
    int64_t unix_time_ms;
    bool time_valid;
} opp_sht45_sample_record_t;

esp_err_t opp_sht45_monitor_start(bool enabled);
void opp_sht45_monitor_set_enabled(bool enabled);
bool opp_sht45_monitor_is_enabled(void);

bool opp_sht45_monitor_get_latest(opp_sht45_sample_record_t *sample, int64_t *sample_age_ms);