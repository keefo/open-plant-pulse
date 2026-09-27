#include "battery_monitor.h"

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ina219.h"
#include "ina219_decode.h"
#include "sdkconfig.h"

#if CONFIG_OPP_BATTERY_MONITOR_ENABLED

#define BATTERY_TASK_STACK_SIZE 3072
#define BATTERY_TASK_PRIORITY 4
#define BATTERY_RETRY_INTERVAL_MS 10000
#define BATTERY_LOG_INTERVAL_US (60LL * 1000000LL)
/* Change whenever opp_battery_gauge_t changes layout or meaning, so a retained
 * gauge from older firmware is discarded rather than misread. */
#define BATTERY_GAUGE_MAGIC 0x42415432U /* "BAT2" */

static const char *TAG = "battery";

/* Survives resets and deep sleep but not a power loss, and a power loss means
 * the pack was disconnected, which invalidates the count anyway. The magic
 * tells a retained gauge from power-on noise. */
static RTC_NOINIT_ATTR struct {
    uint32_t magic;
    opp_battery_gauge_t gauge;
} retained;

static portMUX_TYPE status_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t monitor_task;
static opp_battery_status_t latest;
static int64_t latest_time_us;
static bool latest_valid;
static esp_err_t last_error = ESP_ERR_INVALID_STATE;

static float signed_current_ma(const opp_ina219_sample_t *sample)
{
    const float current = opp_ina219_current_ma(sample->shunt_microvolts,
                                                CONFIG_OPP_INA219_SHUNT_MILLIOHMS);
#if CONFIG_OPP_INA219_POSITIVE_IS_CHARGING
    return current;
#else
    return -current;
#endif
}

static void publish(esp_err_t error, const opp_ina219_sample_t *sample, float current_ma)
{
    opp_battery_status_t status = {0};
    if (error == ESP_OK) {
        const opp_battery_gauge_t *gauge = &retained.gauge;
        status.millivolts = sample->bus_millivolts;
        status.current_ma = current_ma;
        status.power_mw = (float)sample->bus_millivolts * current_ma / 1000.0f;
        status.overflow = sample->overflow;
        status.flow = opp_battery_classify(current_ma);
        status.percent = opp_battery_gauge_percent(gauge);
        status.voltage_percent = opp_battery_percent_from_millivolts(sample->bus_millivolts);
        status.calibrated = gauge->calibrated;
        status.average_current_ma = gauge->average_current_ma;
        status.hours_to_empty_valid =
            opp_battery_gauge_hours_to_empty(gauge, &status.hours_to_empty);
        status.hours_to_full_valid =
            opp_battery_gauge_hours_to_full(gauge, &status.hours_to_full);
    }
    const int64_t now_us = esp_timer_get_time();
    portENTER_CRITICAL(&status_lock);
    last_error = error;
    latest_valid = error == ESP_OK;
    if (latest_valid) {
        latest = status;
        latest_time_us = now_us;
    }
    portEXIT_CRITICAL(&status_lock);
}

static void battery_monitor_task(void *context)
{
    (void)context;
    if (retained.magic != BATTERY_GAUGE_MAGIC ||
        retained.gauge.capacity_mah != (float)CONFIG_OPP_BATTERY_CAPACITY_MAH) {
        opp_battery_gauge_init(&retained.gauge, (float)CONFIG_OPP_BATTERY_CAPACITY_MAH);
        retained.magic = BATTERY_GAUGE_MAGIC;
    } else if (retained.gauge.seeded) {
        ESP_LOGI(TAG, "Continuing retained battery count at %.1f%% (%s)",
                 opp_battery_gauge_percent(&retained.gauge),
                 retained.gauge.calibrated ? "calibrated" : "estimated");
    }

    bool initialised = false;
    esp_err_t previous_error = ESP_OK;
    bool overflow_logged = false;
    int64_t previous_us = 0;
    int64_t last_log_us = -BATTERY_LOG_INTERVAL_US;
    while (true) {
        esp_err_t error = ESP_OK;
        if (!initialised) {
            error = opp_ina219_init(CONFIG_OPP_INA219_ADDRESS);
            if (error == ESP_OK) {
                initialised = true;
                previous_us = 0;
                ESP_LOGI(TAG, "INA219 found at 0x%02x; shunt %d mOhm, capacity %d mAh",
                         CONFIG_OPP_INA219_ADDRESS, CONFIG_OPP_INA219_SHUNT_MILLIOHMS,
                         CONFIG_OPP_BATTERY_CAPACITY_MAH);
            }
        }

        opp_ina219_sample_t sample = {0};
        float current_ma = 0.0f;
        if (error == ESP_OK) {
            error = opp_ina219_read(&sample);
        }
        if (error == ESP_OK) {
            const int64_t now_us = esp_timer_get_time();
            current_ma = signed_current_ma(&sample);
            /* The first read after (re)connecting has no interval to count. */
            const float elapsed_s = previous_us == 0 ? 0.0f
                                                     : (float)(now_us - previous_us) / 1e6f;
            previous_us = now_us;
            opp_battery_gauge_update(&retained.gauge, sample.bus_millivolts, current_ma,
                                     elapsed_s);
            if (sample.overflow && !overflow_logged) {
                ESP_LOGW(TAG, "INA219 reports overflow: current beyond the shunt's range");
            }
            overflow_logged = sample.overflow;
            if (now_us - last_log_us >= BATTERY_LOG_INTERVAL_US) {
                last_log_us = now_us;
                ESP_LOGI(TAG, "%.3f V %+.1f mA %s, %.1f%% (%s)",
                         sample.bus_millivolts / 1000.0, current_ma,
                         opp_battery_flow_name(opp_battery_classify(current_ma)),
                         opp_battery_gauge_percent(&retained.gauge),
                         retained.gauge.calibrated ? "calibrated" : "estimated");
            }
        } else {
            if (initialised) {
                opp_ina219_deinit();
                initialised = false;
            }
            if (error != previous_error) {
                ESP_LOGW(TAG, "INA219 unavailable at 0x%02x: %s", CONFIG_OPP_INA219_ADDRESS,
                         esp_err_to_name(error));
            }
        }
        publish(error, &sample, current_ma);
        previous_error = error;
        vTaskDelay(pdMS_TO_TICKS(error == ESP_OK ? CONFIG_OPP_BATTERY_SAMPLE_INTERVAL_MS
                                                 : BATTERY_RETRY_INTERVAL_MS));
    }
}

esp_err_t opp_battery_monitor_start(void)
{
    if (monitor_task != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    BaseType_t created = xTaskCreate(battery_monitor_task, "battery", BATTERY_TASK_STACK_SIZE,
                                     NULL, BATTERY_TASK_PRIORITY, &monitor_task);
    return created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

bool opp_battery_monitor_is_running(void)
{
    return monitor_task != NULL;
}

bool opp_battery_monitor_get(opp_battery_status_t *status, int64_t *sample_age_ms)
{
    if (status == NULL || sample_age_ms == NULL) {
        return false;
    }
    int64_t time_us = 0;
    portENTER_CRITICAL(&status_lock);
    const bool valid = latest_valid;
    if (valid) {
        *status = latest;
        time_us = latest_time_us;
    }
    portEXIT_CRITICAL(&status_lock);
    if (!valid) {
        return false;
    }
    *sample_age_ms = (esp_timer_get_time() - time_us) / 1000;
    return true;
}

esp_err_t opp_battery_monitor_last_error(void)
{
    portENTER_CRITICAL(&status_lock);
    const esp_err_t error = last_error;
    portEXIT_CRITICAL(&status_lock);
    return error;
}

#else

esp_err_t opp_battery_monitor_start(void) { return ESP_ERR_NOT_SUPPORTED; }
bool opp_battery_monitor_is_running(void) { return false; }
bool opp_battery_monitor_get(opp_battery_status_t *status, int64_t *sample_age_ms)
{
    (void)status;
    (void)sample_age_ms;
    return false;
}
esp_err_t opp_battery_monitor_last_error(void) { return ESP_ERR_NOT_SUPPORTED; }

#endif
