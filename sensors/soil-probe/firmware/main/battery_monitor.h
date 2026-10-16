#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "battery_gauge.h"
#include "esp_err.h"

typedef struct {
    int32_t millivolts;
    /* Positive into the pack (charging), negative out of it. */
    float current_ma;
    float power_mw;
    bool overflow;
    opp_battery_flow_t flow;
    float percent;
    float voltage_percent;
    bool calibrated;
    bool hours_to_empty_valid;
    float hours_to_empty;
    bool hours_to_full_valid;
    float hours_to_full;
    float average_current_ma;
} opp_battery_status_t;

esp_err_t opp_battery_monitor_start(void);
bool opp_battery_monitor_is_running(void);
bool opp_battery_monitor_get(opp_battery_status_t *status, int64_t *sample_age_ms);
/* Why the last read failed, or ESP_OK. */
esp_err_t opp_battery_monitor_last_error(void);
