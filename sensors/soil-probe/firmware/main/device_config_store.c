#include "device_config_store.h"

#include <string.h>

#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "web_ui.h"
#include "wifi_credentials_store.h"
#include "sdkconfig.h"

#define CONFIG_NAMESPACE "device_cfg"
#define CONFIG_KEY "current"

static const char *TAG = "device_config";

/* Switching the console off has to reach the radio, not just the record. The
 * sensor leaves the network, stops serving, and forgets the household password,
 * because a console nobody asked for should not keep a secret it cannot use. */
static void apply_console_setting(bool enabled)
{
    if (enabled) {
        esp_err_t error = web_ui_resume();
        if (error != ESP_OK) {
            ESP_LOGW(TAG, "Could not start the console: %s", esp_err_to_name(error));
        }
        return;
    }
    web_ui_stop();
    esp_err_t error = opp_wifi_credentials_store_clear();
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "Could not forget the network: %s", esp_err_to_name(error));
    }
}
static opp_device_config_t current_config = {
    .reporting_interval_seconds = CONFIG_OPP_SAMPLE_INTERVAL_MINUTES * 60U,
};
static SemaphoreHandle_t config_mutex;

static void set_default_config(void)
{
    memset(&current_config, 0, sizeof(current_config));
    current_config.reporting_interval_seconds = CONFIG_OPP_SAMPLE_INTERVAL_MINUTES * 60U;
}

esp_err_t opp_device_config_store_init(void)
{
    if (config_mutex == NULL) {
        config_mutex = xSemaphoreCreateMutex();
        if (config_mutex == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    set_default_config();

    nvs_handle_t handle = 0;
    esp_err_t error = nvs_open(CONFIG_NAMESPACE, NVS_READONLY, &handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(error, TAG, "Could not open device configuration");

    uint8_t payload[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE];
    size_t payload_size = sizeof(payload);
    error = nvs_get_blob(handle, CONFIG_KEY, payload, &payload_size);
    nvs_close(handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(error, TAG, "Could not read device configuration");

    opp_device_config_t loaded;
    if (!opp_device_config_decode(payload, payload_size, &loaded)) {
        ESP_LOGW(TAG, "Stored device configuration is invalid; using firmware default");
        return ESP_OK;
    }
    current_config = loaded;
    ESP_LOGI(TAG, "Loaded device configuration revision %lu for '%s' in '%s'; interval=%lu s",
             (unsigned long)loaded.revision, loaded.plant_name, loaded.room,
             (unsigned long)loaded.reporting_interval_seconds);
    return ESP_OK;
}

void opp_device_config_store_get(opp_device_config_t *config)
{
    if (config == NULL) {
        return;
    }
    if (config_mutex == NULL) {
        *config = current_config;
        return;
    }
    xSemaphoreTake(config_mutex, portMAX_DELAY);
    *config = current_config;
    xSemaphoreGive(config_mutex);
}

esp_err_t opp_device_config_store_apply(const opp_device_config_t *config)
{
    uint8_t payload[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE];
    const size_t payload_size = opp_device_config_encode(config, payload);
    if (payload_size == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    xSemaphoreTake(config_mutex, portMAX_DELAY);
    if (opp_device_config_equal(config, &current_config)) {
        xSemaphoreGive(config_mutex);
        return ESP_OK;
    }
    if (current_config.revision != 0 && config->revision <= current_config.revision) {
        xSemaphoreGive(config_mutex);
        return ESP_ERR_INVALID_STATE;
    }

    nvs_handle_t handle = 0;
    esp_err_t error = nvs_open(CONFIG_NAMESPACE, NVS_READWRITE, &handle);
    if (error == ESP_OK) {
        error = nvs_set_blob(handle, CONFIG_KEY, payload, payload_size);
    }
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    if (error == ESP_OK) {
        const bool console_changed = current_config.console_enabled != config->console_enabled;
        current_config = *config;
        if (console_changed) {
            apply_console_setting(config->console_enabled);
        }
        ESP_LOGI(TAG,
                 "Applied device configuration revision %lu for '%s' in '%s'; interval=%lu s",
                 (unsigned long)config->revision, config->plant_name, config->room,
                 (unsigned long)config->reporting_interval_seconds);
    }
    xSemaphoreGive(config_mutex);
    return error;
}
esp_err_t opp_device_config_store_clear(void)
{
    nvs_handle_t handle;
    esp_err_t error = nvs_open(CONFIG_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_erase_key(handle, CONFIG_KEY);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        error = ESP_OK;
    }
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    if (error == ESP_OK) {
        memset(&current_config, 0, sizeof(current_config));
        ESP_LOGI(TAG, "Forgot the plant, room and interval");
    }
    return error;
}
