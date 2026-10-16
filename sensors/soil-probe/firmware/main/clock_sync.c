#include "clock_sync.h"

#include <stdlib.h>
#include <sys/time.h>
#include <time.h>

#include "clock_policy.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#define CLOCK_TASK_STACK_SIZE 4096
#define CLOCK_TASK_PRIORITY 4
#define CLOCK_CHECK_INTERVAL_MS (60 * 1000)
#define RTC_CLOCK_MARKER UINT32_C(0x4f505043)

static const char *TAG = "clock_sync";
static portMUX_TYPE clock_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t clock_task;
static bool network_available;
static bool sync_in_progress;
static bool adjustment_known;
static int64_t last_adjustment_ms;
static uint32_t sync_failures;
static RTC_DATA_ATTR uint32_t rtc_clock_marker;
static RTC_DATA_ATTR int64_t rtc_last_sync_unix_s;
static RTC_DATA_ATTR int64_t rtc_next_attempt_unix_s;

static int64_t timeval_to_ms(const struct timeval *value)
{
    return (int64_t)value->tv_sec * 1000 + value->tv_usec / 1000;
}

static bool sync_due_at(time_t now, uint32_t marker, int64_t last_sync_unix_s)
{
#if CONFIG_OPP_CLOCK_SYNC_ENABLED
    return opp_clock_resync_is_due(
        now, marker == RTC_CLOCK_MARKER ? last_sync_unix_s : 0,
        (int64_t)CONFIG_OPP_CLOCK_RESYNC_INTERVAL_HOURS * 60 * 60);
#else
    (void)now;
    (void)marker;
    (void)last_sync_unix_s;
    return false;
#endif
}

bool opp_clock_capture_timestamp(int64_t *unix_time_ms)
{
    if (unix_time_ms == NULL) {
        return false;
    }

    struct timeval now;
    gettimeofday(&now, NULL);
    if (!opp_clock_epoch_is_plausible(now.tv_sec)) {
        *unix_time_ms = 0;
        return false;
    }
    *unix_time_ms = timeval_to_ms(&now);
    return true;
}

bool opp_clock_sync_needed(void)
{
#if !CONFIG_OPP_CLOCK_SYNC_ENABLED
    return false;
#else
    time_t now;
    time(&now);
    bool ready;
    portENTER_CRITICAL(&clock_lock);
    ready = sync_due_at(now, rtc_clock_marker, rtc_last_sync_unix_s) &&
            !sync_in_progress && now >= rtc_next_attempt_unix_s;
    portEXIT_CRITICAL(&clock_lock);
    return ready;
#endif
}

esp_err_t opp_clock_sync_now(void)
{
#if !CONFIG_OPP_CLOCK_SYNC_ENABLED
    return ESP_ERR_NOT_SUPPORTED;
#else
    portENTER_CRITICAL(&clock_lock);
    if (sync_in_progress) {
        portEXIT_CRITICAL(&clock_lock);
        return ESP_ERR_INVALID_STATE;
    }
    sync_in_progress = true;
    portEXIT_CRITICAL(&clock_lock);

    struct timeval before;
    gettimeofday(&before, NULL);
    const bool before_valid = opp_clock_epoch_is_plausible(before.tv_sec);
    const int64_t started_us = esp_timer_get_time();

    ESP_LOGI(TAG, "Starting bounded SNTP synchronization with %s", CONFIG_OPP_SNTP_SERVER);
    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_OPP_SNTP_SERVER);
    esp_err_t error = esp_netif_sntp_init(&config);
    if (error == ESP_OK) {
        error = esp_netif_sntp_sync_wait(
            pdMS_TO_TICKS(CONFIG_OPP_CLOCK_SYNC_TIMEOUT_SECONDS * 1000));
        esp_netif_sntp_deinit();
    }

    struct timeval after;
    gettimeofday(&after, NULL);
    if (error == ESP_OK && !opp_clock_epoch_is_plausible(after.tv_sec)) {
        error = ESP_ERR_INVALID_RESPONSE;
    }

    portENTER_CRITICAL(&clock_lock);
    sync_in_progress = false;
    if (error == ESP_OK) {
        rtc_clock_marker = RTC_CLOCK_MARKER;
        rtc_last_sync_unix_s = after.tv_sec;
        if (before_valid) {
            last_adjustment_ms = opp_clock_adjustment_ms(
                timeval_to_ms(&before), esp_timer_get_time() - started_us,
                timeval_to_ms(&after));
            adjustment_known = true;
        } else {
            last_adjustment_ms = 0;
            adjustment_known = false;
        }
        sync_failures = 0;
        rtc_next_attempt_unix_s = 0;
    } else {
        sync_failures++;
        rtc_next_attempt_unix_s = after.tv_sec +
            (int64_t)CONFIG_OPP_CLOCK_SYNC_RETRY_MINUTES * 60;
    }
    const bool correction_known = adjustment_known;
    const int64_t correction_ms = last_adjustment_ms;
    portEXIT_CRITICAL(&clock_lock);

    if (error != ESP_OK) {
        ESP_LOGW(TAG, "SNTP synchronization failed: %s; timestamps remain %s",
                 esp_err_to_name(error),
                 opp_clock_epoch_is_plausible(after.tv_sec) ? "RTC-derived" : "sequence-only");
        return error;
    }

    ESP_LOGI(TAG, "System clock synchronized at Unix time %lld", (long long)after.tv_sec);
    if (correction_known &&
        llabs(correction_ms) >= (int64_t)CONFIG_OPP_CLOCK_JUMP_WARNING_SECONDS * 1000) {
        ESP_LOGW(TAG, "System clock corrected by %+lld ms; existing sample timestamps are unchanged",
                 (long long)correction_ms);
    }
    return ESP_OK;
#endif
}

static void clock_sync_task(void *context)
{
    (void)context;
    while (true) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(CLOCK_CHECK_INTERVAL_MS));
        portENTER_CRITICAL(&clock_lock);
        const bool connected = network_available;
        portEXIT_CRITICAL(&clock_lock);
        if (connected && opp_clock_sync_needed()) {
            opp_clock_sync_now();
        }
    }
}

esp_err_t opp_clock_sync_init(void)
{
    time_t now;
    time(&now);
    if (!opp_clock_epoch_is_plausible(now)) {
        rtc_clock_marker = 0;
        rtc_last_sync_unix_s = 0;
        ESP_LOGW(TAG, "System clock is not plausible; samples use sequence-relative time until SNTP succeeds");
    } else if (rtc_clock_marker == RTC_CLOCK_MARKER) {
        ESP_LOGI(TAG, "Retained RTC time is plausible (Unix time %lld)", (long long)now);
    } else {
        ESP_LOGI(TAG, "System clock is plausible but has no retained SNTP marker");
    }

#if CONFIG_OPP_CLOCK_SYNC_ENABLED
    BaseType_t created = xTaskCreate(clock_sync_task, "clock_sync", CLOCK_TASK_STACK_SIZE,
                                     NULL, CLOCK_TASK_PRIORITY, &clock_task);
    return created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
#else
    return ESP_OK;
#endif
}

void opp_clock_sync_set_network_available(bool available)
{
    portENTER_CRITICAL(&clock_lock);
    network_available = available;
    portEXIT_CRITICAL(&clock_lock);
    if (available && clock_task != NULL) {
        xTaskNotifyGive(clock_task);
    }
}

void opp_clock_get_status(opp_clock_status_t *status)
{
    if (status == NULL) {
        return;
    }

    time_t now;
    time(&now);
    portENTER_CRITICAL(&clock_lock);
    *status = (opp_clock_status_t) {
        .time_valid = opp_clock_epoch_is_plausible(now),
        .sync_in_progress = sync_in_progress,
        .sync_due = sync_due_at(now, rtc_clock_marker, rtc_last_sync_unix_s),
        .rtc_retained = rtc_clock_marker == RTC_CLOCK_MARKER,
        .adjustment_known = adjustment_known,
        .unix_time_s = now,
        .last_sync_unix_s = rtc_clock_marker == RTC_CLOCK_MARKER
            ? rtc_last_sync_unix_s : 0,
        .last_adjustment_ms = last_adjustment_ms,
        .sync_failures = sync_failures,
    };
    portEXIT_CRITICAL(&clock_lock);
}