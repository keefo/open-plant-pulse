#include "report_delivery.h"

#include <stdio.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "sdkconfig.h"

#define DELIVERY_NAMESPACE "rqueue"
#define COUNTERS_KEY "counters"

static const char *TAG = "delivery";
static StaticSemaphore_t lock_storage;
static SemaphoreHandle_t lock;
static opp_report_queue_t queue;
static bool opened;

static bool with_handle(nvs_open_mode_t mode, nvs_handle_t *handle)
{
    return nvs_open(DELIVERY_NAMESPACE, mode, handle) == ESP_OK;
}

static bool load_counters(void *context, uint32_t *head, uint32_t *tail)
{
    (void)context;
    nvs_handle_t handle;
    if (!with_handle(NVS_READONLY, &handle)) {
        return false;
    }
    uint32_t counters[2];
    size_t size = sizeof(counters);
    const bool loaded = nvs_get_blob(handle, COUNTERS_KEY, counters, &size) == ESP_OK &&
                        size == sizeof(counters);
    nvs_close(handle);
    if (loaded) {
        *head = counters[0];
        *tail = counters[1];
    }
    return loaded;
}

/* One blob for both counters, so moving the head or the tail is a single NVS
 * write that either happens or does not. */
static bool save_counters(void *context, uint32_t head, uint32_t tail)
{
    (void)context;
    nvs_handle_t handle;
    if (!with_handle(NVS_READWRITE, &handle)) {
        return false;
    }
    const uint32_t counters[2] = {head, tail};
    const bool saved = nvs_set_blob(handle, COUNTERS_KEY, counters, sizeof(counters)) == ESP_OK &&
                       nvs_commit(handle) == ESP_OK;
    nvs_close(handle);
    return saved;
}

static void slot_key(uint32_t slot, char key[8])
{
    snprintf(key, 8, "r%02lu", (unsigned long)slot);
}

static bool load_entry(void *context, uint32_t slot, opp_queued_report_t *entry)
{
    (void)context;
    nvs_handle_t handle;
    if (!with_handle(NVS_READONLY, &handle)) {
        return false;
    }
    char key[8];
    slot_key(slot, key);
    size_t size = sizeof(*entry);
    const bool loaded = nvs_get_blob(handle, key, entry, &size) == ESP_OK &&
                        size == sizeof(*entry);
    nvs_close(handle);
    return loaded;
}

static bool save_entry(void *context, uint32_t slot, const opp_queued_report_t *entry)
{
    (void)context;
    nvs_handle_t handle;
    if (!with_handle(NVS_READWRITE, &handle)) {
        return false;
    }
    char key[8];
    slot_key(slot, key);
    const bool saved = nvs_set_blob(handle, key, entry, sizeof(*entry)) == ESP_OK &&
                       nvs_commit(handle) == ESP_OK;
    nvs_close(handle);
    return saved;
}

esp_err_t opp_delivery_init(void)
{
    if (lock == NULL) {
        lock = xSemaphoreCreateMutexStatic(&lock_storage);
    }
    const opp_report_queue_storage_t storage = {
        .load_counters = load_counters,
        .save_counters = save_counters,
        .load_entry = load_entry,
        .save_entry = save_entry,
    };
    xSemaphoreTake(lock, portMAX_DELAY);
    opened = opp_report_queue_open(&queue, &storage, CONFIG_OPP_REPORT_QUEUE_CAPACITY);
    const uint32_t depth = opp_report_queue_depth(&queue);
    xSemaphoreGive(lock);
    if (!opened) {
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "%lu report(s) awaiting the hub of %d", (unsigned long)depth,
             CONFIG_OPP_REPORT_QUEUE_CAPACITY);
    return ESP_OK;
}

bool opp_delivery_push(const opp_queued_report_t *report)
{
    if (!opened) {
        return false;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    const bool pushed = opp_report_queue_push(&queue, report);
    xSemaphoreGive(lock);
    return pushed;
}

bool opp_delivery_head(opp_queued_report_t *report)
{
    if (!opened) {
        return false;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    const bool found = opp_report_queue_head(&queue, report);
    xSemaphoreGive(lock);
    return found;
}

opp_report_ack_result_t opp_delivery_acknowledge(uint32_t report_id)
{
    if (!opened) {
        return OPP_REPORT_ACK_REFUSED;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    const opp_report_ack_result_t result = opp_report_queue_ack(&queue, report_id);
    const uint32_t depth = opp_report_queue_depth(&queue);
    xSemaphoreGive(lock);
    if (result == OPP_REPORT_ACK_REMOVED) {
        ESP_LOGI(TAG, "Hub acknowledged report %lu; %lu still waiting",
                 (unsigned long)report_id, (unsigned long)depth);
    } else if (result == OPP_REPORT_ACK_REFUSED) {
        ESP_LOGW(TAG, "Refused acknowledgement for report %lu", (unsigned long)report_id);
    }
    return result;
}

uint32_t opp_delivery_depth(void)
{
    if (!opened) {
        return 0;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    const uint32_t depth = opp_report_queue_depth(&queue);
    xSemaphoreGive(lock);
    return depth;
}

uint32_t opp_delivery_capacity(void)
{
    return CONFIG_OPP_REPORT_QUEUE_CAPACITY;
}

bool opp_delivery_clear(void)
{
    if (!opened) {
        return false;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    const bool cleared = opp_report_queue_clear(&queue);
    xSemaphoreGive(lock);
    return cleared;
}
