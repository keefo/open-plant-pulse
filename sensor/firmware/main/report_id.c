#include "report_id.h"

#include "esp_attr.h"
#include "esp_log.h"
#include "nvs.h"

/* Flash holds the first ID not yet reserved. IDs are taken from a reserved
 * block, so flash is written once per block instead of once per report; after
 * a reset the rest of the block is skipped, which only leaves a gap. */
#define REPORT_ID_BLOCK 100U
#define REPORT_ID_NAMESPACE "report"
#define REPORT_ID_LIMIT_KEY "limit"
#define REPORT_ID_MAGIC 0x52494431U /* "RID1" */

static const char *TAG = "report_id";

/* Deep sleep keeps this, so waking does not burn a block per wake. A power
 * loss clears it and the next ID comes from flash. */
static RTC_NOINIT_ATTR struct {
    uint32_t magic;
    uint32_t next;
    uint32_t limit;
} state;

static esp_err_t reserve_from(uint32_t first)
{
    if (first > UINT32_MAX - REPORT_ID_BLOCK) {
        /* Never wraps: an exhausted counter stops reporting instead. */
        return ESP_ERR_INVALID_SIZE;
    }
    nvs_handle_t handle;
    esp_err_t error = nvs_open(REPORT_ID_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    const uint32_t limit = first + REPORT_ID_BLOCK;
    error = nvs_set_u32(handle, REPORT_ID_LIMIT_KEY, limit);
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    if (error == ESP_OK) {
        state.next = first;
        state.limit = limit;
        state.magic = REPORT_ID_MAGIC;
    }
    return error;
}

esp_err_t opp_report_id_init(void)
{
    if (state.magic == REPORT_ID_MAGIC && state.next != 0 && state.next <= state.limit) {
        return ESP_OK;
    }
    uint32_t limit = 1;
    nvs_handle_t handle;
    esp_err_t error = nvs_open(REPORT_ID_NAMESPACE, NVS_READONLY, &handle);
    if (error == ESP_OK) {
        error = nvs_get_u32(handle, REPORT_ID_LIMIT_KEY, &limit);
        nvs_close(handle);
    }
    if (error != ESP_OK && error != ESP_ERR_NVS_NOT_FOUND) {
        return error;
    }
    if (limit == 0) {
        limit = 1;
    }
    error = reserve_from(limit);
    if (error == ESP_OK) {
        ESP_LOGI(TAG, "Report IDs continue from %lu", (unsigned long)state.next);
    }
    return error;
}

uint32_t opp_report_id_next(void)
{
    if (state.magic != REPORT_ID_MAGIC) {
        return 0;
    }
    if (state.next >= state.limit) {
        const esp_err_t error = reserve_from(state.limit);
        if (error != ESP_OK) {
            ESP_LOGE(TAG, "Could not reserve report IDs: %s", esp_err_to_name(error));
            return 0;
        }
    }
    return state.next++;
}
