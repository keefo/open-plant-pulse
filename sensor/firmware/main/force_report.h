#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"

typedef enum {
    OPP_FORCE_REPORT_IDLE,
    OPP_FORCE_REPORT_QUEUED,
    OPP_FORCE_REPORT_REPORTING,
    OPP_FORCE_REPORT_ACKNOWLEDGED,
    OPP_FORCE_REPORT_UNACKNOWLEDGED,
    OPP_FORCE_REPORT_FAILED,
} opp_force_report_state_t;

typedef enum {
    OPP_FORCE_REPORT_FAILURE_NONE,
    OPP_FORCE_REPORT_FAILURE_NO_SAMPLE,
    OPP_FORCE_REPORT_FAILURE_ENCODING,
    OPP_FORCE_REPORT_FAILURE_BROADCAST,
    OPP_FORCE_REPORT_FAILURE_QUEUE_FULL,
} opp_force_report_failure_t;

typedef struct {
    opp_force_report_state_t state;
    opp_force_report_failure_t failure;
    uint32_t request_id;
    bool report_id_valid;
    uint32_t report_id;
    int64_t requested_at_ms;
    int64_t report_completed_at_ms;
    int64_t acknowledged_at_ms;
} opp_force_report_status_t;

esp_err_t opp_force_report_init(void);
esp_err_t opp_force_report_request(uint32_t *request_id);
bool opp_force_report_wait(TickType_t timeout_ticks, uint32_t *request_id);
void opp_force_report_started(uint32_t request_id, uint32_t report_id);
void opp_force_report_failed(uint32_t request_id, opp_force_report_failure_t failure);
void opp_force_report_finished(uint32_t request_id, esp_err_t broadcast_result, bool queued);
void opp_force_report_get_status(opp_force_report_status_t *status);
/* The hub acknowledged this report; if it is the forced one, it is done. */
void opp_force_report_delivered(uint32_t report_id);
const char *opp_force_report_state_name(opp_force_report_state_t state);
const char *opp_force_report_failure_name(opp_force_report_failure_t failure);