#include "wifi_credentials_store.h"

#include <inttypes.h>
#include <string.h>

#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "wifi_store";

#define CREDENTIALS_NAMESPACE "device_cfg"
#define CREDENTIALS_KEY "wifi"

static opp_wifi_credentials_t current;

esp_err_t opp_wifi_credentials_store_init(void)
{
    nvs_handle_t handle;
    esp_err_t error = nvs_open(CREDENTIALS_NAMESPACE, NVS_READONLY, &handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (error != ESP_OK) {
        return error;
    }

    uint8_t payload[OPP_WIFI_CREDENTIALS_PAYLOAD_MAX_SIZE];
    size_t payload_size = sizeof(payload);
    error = nvs_get_blob(handle, CREDENTIALS_KEY, payload, &payload_size);
    nvs_close(handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (error != ESP_OK) {
        return error;
    }

    opp_wifi_credentials_t loaded;
    if (!opp_wifi_credentials_decode(payload, payload_size, &loaded)) {
        ESP_LOGW(TAG, "Stored network was unreadable and has been ignored");
        return ESP_OK;
    }
    current = loaded;
    /* Deliberately without the network name: a log that names the household
     * network is one step from a log that names its password. */
    ESP_LOGI(TAG, "Loaded network revision %" PRIu32 "; console %s",
             current.revision, current.enabled ? "on" : "off");
    return ESP_OK;
}

void opp_wifi_credentials_store_get(opp_wifi_credentials_t *credentials)
{
    if (credentials != NULL) {
        *credentials = current;
    }
}

esp_err_t opp_wifi_credentials_store_apply(const opp_wifi_credentials_t *credentials)
{
    if (credentials == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (current.revision != 0 && credentials->revision <= current.revision) {
        return ESP_OK;
    }

    uint8_t payload[OPP_WIFI_CREDENTIALS_PAYLOAD_MAX_SIZE];
    const size_t payload_size = opp_wifi_credentials_encode(credentials, payload);
    if (payload_size == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t handle;
    esp_err_t error = nvs_open(CREDENTIALS_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_set_blob(handle, CREDENTIALS_KEY, payload, payload_size);
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    if (error == ESP_OK) {
        current = *credentials;
        ESP_LOGI(TAG, "Applied network revision %" PRIu32 "; console %s",
                 current.revision, current.enabled ? "on" : "off");
    }
    return error;
}

esp_err_t opp_wifi_credentials_store_clear(void)
{
    nvs_handle_t handle;
    esp_err_t error = nvs_open(CREDENTIALS_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_erase_key(handle, CREDENTIALS_KEY);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        error = ESP_OK;
    }
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    if (error == ESP_OK) {
        memset(&current, 0, sizeof(current));
        ESP_LOGI(TAG, "Forgot the household network");
    }
    return error;
}
