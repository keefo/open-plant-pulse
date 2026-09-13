#include "sht45_monitor.h"

#include "clock_sync.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#define SHT45_TASK_STACK_SIZE 3072
#define SHT45_TASK_PRIORITY 5

static const char *TAG = "sht45";
static portMUX_TYPE sample_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t monitor_task;
static opp_sht45_sample_record_t latest_sample;
static int64_t latest_sample_time_us;
static bool latest_sample_valid;
static bool monitor_enabled;
static RTC_DATA_ATTR uint64_t sample_sequence;

static bool set_latest_sample(const opp_sht45_sample_t *sample)
{
    const int64_t sample_time_us = esp_timer_get_time();
    int64_t unix_time_ms;
    const bool time_valid = opp_clock_capture_timestamp(&unix_time_ms);

    portENTER_CRITICAL(&sample_lock);
    if (!monitor_enabled) {
        portEXIT_CRITICAL(&sample_lock);
        return false;
    }
    latest_sample_time_us = sample_time_us;
    latest_sample.values = *sample;
    latest_sample.sequence = ++sample_sequence;
    latest_sample.monotonic_ms = latest_sample_time_us / 1000;
    latest_sample.unix_time_ms = unix_time_ms;
    latest_sample.time_valid = time_valid;
    latest_sample_valid = true;
    portEXIT_CRITICAL(&sample_lock);
    return true;
}

static void invalidate_latest_sample(void)
{
    portENTER_CRITICAL(&sample_lock);
    latest_sample_valid = false;
    portEXIT_CRITICAL(&sample_lock);
}

static void sht45_monitor_task(void *context)
{
    (void)context;

    bool bus_initialised = false;
    bool sensor_detected = false;
    bool disabled_logged = false;
    while (true) {
        if (!opp_sht45_monitor_is_enabled()) {
            if (bus_initialised) {
                sensor_detected = false;
                esp_err_t error = opp_sht45_deinit();
                if (error != ESP_OK) {
                    ESP_LOGW(TAG, "Failed to deinitialise SHT45 I2C bus: %s",
                             esp_err_to_name(error));
                } else {
                    bus_initialised = false;
                }
            }
            if (!disabled_logged) {
                ESP_LOGI(TAG, "SHT45 sampling disabled");
                disabled_logged = true;
            }
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }
        disabled_logged = false;

        if (!bus_initialised) {
            esp_err_t init_error = opp_sht45_init();
            if (init_error != ESP_OK) {
                invalidate_latest_sample();
                ESP_LOGW(TAG, "Could not initialise SHT45 I2C bus: %s",
                         esp_err_to_name(init_error));
                ulTaskNotifyTake(pdTRUE,
                                 pdMS_TO_TICKS(CONFIG_OPP_SHT45_SAMPLE_INTERVAL_SECONDS * 1000));
                continue;
            }
            bus_initialised = true;
        }

        if (!sensor_detected) {
            esp_err_t probe_error = opp_sht45_probe();
            if (probe_error != ESP_OK) {
                invalidate_latest_sample();
                ESP_LOGW(TAG, "SHT45 not found at 0x44: %s", esp_err_to_name(probe_error));
                ulTaskNotifyTake(pdTRUE,
                                 pdMS_TO_TICKS(CONFIG_OPP_SHT45_SAMPLE_INTERVAL_SECONDS * 1000));
                continue;
            }
            sensor_detected = true;
            ESP_LOGI(TAG, "SHT45 detected at 0x44 on SDA GPIO%d, SCL GPIO%d",
                     CONFIG_OPP_SHT45_SDA_GPIO, CONFIG_OPP_SHT45_SCL_GPIO);
        }

        opp_sht45_sample_t sample;
        esp_err_t read_error = opp_sht45_read(&sample);
        if (read_error == ESP_OK) {
            if (set_latest_sample(&sample)) {
                ESP_LOGI(TAG, "SHT45 air_temperature_c=%.2f air_humidity_percent=%.2f",
                         sample.air_temperature_c, sample.air_humidity_percent);
            }
        } else {
            invalidate_latest_sample();
            ESP_LOGW(TAG, "SHT45 measurement failed: %s", esp_err_to_name(read_error));
        }

        ulTaskNotifyTake(pdTRUE,
                         pdMS_TO_TICKS(CONFIG_OPP_SHT45_SAMPLE_INTERVAL_SECONDS * 1000));
    }
}

esp_err_t opp_sht45_monitor_start(bool enabled)
{
    if (monitor_task != NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    portENTER_CRITICAL(&sample_lock);
    monitor_enabled = enabled;
    latest_sample_valid = false;
    portEXIT_CRITICAL(&sample_lock);

    BaseType_t task_created = xTaskCreate(sht45_monitor_task, "sht45_monitor",
                                          SHT45_TASK_STACK_SIZE, NULL,
                                          SHT45_TASK_PRIORITY, &monitor_task);
    return task_created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

void opp_sht45_monitor_set_enabled(bool enabled)
{
    portENTER_CRITICAL(&sample_lock);
    monitor_enabled = enabled;
    if (!enabled) {
        latest_sample_valid = false;
    }
    TaskHandle_t task = monitor_task;
    portEXIT_CRITICAL(&sample_lock);

    if (task != NULL) {
        xTaskNotifyGive(task);
    }
}

bool opp_sht45_monitor_is_enabled(void)
{
    portENTER_CRITICAL(&sample_lock);
    const bool enabled = monitor_enabled;
    portEXIT_CRITICAL(&sample_lock);
    return enabled;
}

bool opp_sht45_monitor_get_latest(opp_sht45_sample_record_t *sample, int64_t *sample_age_ms)
{
    if (sample == NULL || sample_age_ms == NULL) {
        return false;
    }

    int64_t sample_time_us;
    portENTER_CRITICAL(&sample_lock);
    const bool valid = latest_sample_valid;
    if (valid) {
        *sample = latest_sample;
        sample_time_us = latest_sample_time_us;
    }
    portEXIT_CRITICAL(&sample_lock);

    if (!valid) {
        return false;
    }
    *sample_age_ms = (esp_timer_get_time() - sample_time_us) / 1000;
    return true;
}