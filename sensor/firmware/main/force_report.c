#include "force_report.h"

#include <string.h>

#include "esp_timer.h"
#include "freertos/queue.h"

static QueueHandle_t report_queue;
static portMUX_TYPE status_lock = portMUX_INITIALIZER_UNLOCKED;
static opp_force_report_status_t report_status;
static uint32_t next_request_id;

static int64_t monotonic_ms(void)
{
    return esp_timer_get_time() / 1000;
}

esp_err_t opp_force_report_init(void)
{
    if (report_queue != NULL) {
        return ESP_OK;
    }
    report_queue = xQueueCreate(1, sizeof(uint32_t));
    if (report_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    memset(&report_status, 0, sizeof(report_status));
    report_status.state = OPP_FORCE_REPORT_IDLE;
    return ESP_OK;
}

esp_err_t opp_force_report_request(uint32_t *request_id)
{
    if (report_queue == NULL || request_id == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    uint32_t requested_id;
    portENTER_CRITICAL(&status_lock);
    if (report_status.state == OPP_FORCE_REPORT_QUEUED ||
        report_status.state == OPP_FORCE_REPORT_REPORTING) {
        portEXIT_CRITICAL(&status_lock);
        return ESP_ERR_INVALID_STATE;
    }
    next_request_id++;
    if (next_request_id == 0) {
        next_request_id++;
    }
    requested_id = next_request_id;
    memset(&report_status, 0, sizeof(report_status));
    report_status.state = OPP_FORCE_REPORT_QUEUED;
    report_status.request_id = requested_id;
    report_status.requested_at_ms = monotonic_ms();
    portEXIT_CRITICAL(&status_lock);

    if (xQueueSend(report_queue, &requested_id, 0) != pdTRUE) {
        opp_force_report_failed(requested_id, OPP_FORCE_REPORT_FAILURE_BROADCAST);
        return ESP_ERR_TIMEOUT;
    }
    *request_id = requested_id;
    return ESP_OK;
}

bool opp_force_report_wait(TickType_t timeout_ticks, uint32_t *request_id)
{
    return report_queue != NULL && request_id != NULL &&
           xQueueReceive(report_queue, request_id, timeout_ticks) == pdTRUE;
}

void opp_force_report_started(uint32_t request_id, uint8_t packet_id)
{
    portENTER_CRITICAL(&status_lock);
    if (report_status.request_id == request_id &&
        report_status.state == OPP_FORCE_REPORT_QUEUED) {
        report_status.state = OPP_FORCE_REPORT_REPORTING;
        report_status.packet_id_valid = true;
        report_status.packet_id = packet_id;
    }
    portEXIT_CRITICAL(&status_lock);
}

void opp_force_report_failed(uint32_t request_id, opp_force_report_failure_t failure)
{
    portENTER_CRITICAL(&status_lock);
    if (report_status.request_id == request_id) {
        report_status.state = OPP_FORCE_REPORT_FAILED;
        report_status.failure = failure;
    }
    portEXIT_CRITICAL(&status_lock);
}

void opp_force_report_finished(uint32_t request_id, esp_err_t broadcast_result)
{
    portENTER_CRITICAL(&status_lock);
    if (report_status.request_id == request_id) {
        report_status.report_completed_at_ms = monotonic_ms();
        if (report_status.state != OPP_FORCE_REPORT_ACKNOWLEDGED) {
            report_status.state = broadcast_result == ESP_OK
                                      ? OPP_FORCE_REPORT_UNACKNOWLEDGED
                                      : OPP_FORCE_REPORT_FAILED;
            report_status.failure = broadcast_result == ESP_OK
                                      ? OPP_FORCE_REPORT_FAILURE_NONE
                                      : OPP_FORCE_REPORT_FAILURE_BROADCAST;
        }
    }
    portEXIT_CRITICAL(&status_lock);
}

void opp_force_report_get_status(opp_force_report_status_t *status)
{
    if (status == NULL) {
        return;
    }
    portENTER_CRITICAL(&status_lock);
    *status = report_status;
    portEXIT_CRITICAL(&status_lock);
}

bool opp_force_report_get_ack(opp_report_ack_t *ack)
{
    if (ack == NULL) {
        return false;
    }
    bool available = false;
    portENTER_CRITICAL(&status_lock);
    if (report_status.request_id != 0 && report_status.packet_id_valid &&
        report_status.state == OPP_FORCE_REPORT_REPORTING) {
        ack->request_id = report_status.request_id;
        ack->packet_id = report_status.packet_id;
        available = true;
    }
    portEXIT_CRITICAL(&status_lock);
    return available;
}

esp_err_t opp_force_report_acknowledge(const opp_report_ack_t *ack)
{
    if (ack == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t result = ESP_ERR_INVALID_STATE;
    portENTER_CRITICAL(&status_lock);
    if (report_status.state == OPP_FORCE_REPORT_REPORTING &&
        report_status.request_id == ack->request_id && report_status.packet_id_valid &&
        report_status.packet_id == ack->packet_id) {
        report_status.state = OPP_FORCE_REPORT_ACKNOWLEDGED;
        report_status.acknowledged_at_ms = monotonic_ms();
        result = ESP_OK;
    } else if (report_status.state == OPP_FORCE_REPORT_ACKNOWLEDGED &&
               report_status.request_id == ack->request_id &&
               report_status.packet_id == ack->packet_id) {
        result = ESP_OK;
    }
    portEXIT_CRITICAL(&status_lock);
    return result;
}

const char *opp_force_report_state_name(opp_force_report_state_t state)
{
    switch (state) {
    case OPP_FORCE_REPORT_IDLE:
        return "idle";
    case OPP_FORCE_REPORT_QUEUED:
        return "queued";
    case OPP_FORCE_REPORT_REPORTING:
        return "reporting";
    case OPP_FORCE_REPORT_ACKNOWLEDGED:
        return "acknowledged";
    case OPP_FORCE_REPORT_UNACKNOWLEDGED:
        return "unacknowledged";
    case OPP_FORCE_REPORT_FAILED:
        return "failed";
    default:
        return "unknown";
    }
}

const char *opp_force_report_failure_name(opp_force_report_failure_t failure)
{
    switch (failure) {
    case OPP_FORCE_REPORT_FAILURE_NONE:
        return "";
    case OPP_FORCE_REPORT_FAILURE_NO_SAMPLE:
        return "no valid sensor sample";
    case OPP_FORCE_REPORT_FAILURE_ENCODING:
        return "payload encoding failed";
    case OPP_FORCE_REPORT_FAILURE_BROADCAST:
        return "BLE broadcast failed";
    default:
        return "unknown failure";
    }
}