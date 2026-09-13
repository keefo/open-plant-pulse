#include "esp_app_desc.h"
#include "esp_log.h"
#include "clock_sync.h"
#include "sht45_monitor.h"
#include "web_ui.h"
#include "web_ui_config.h"

static const char *TAG = "plant_pulse";

void app_main(void)
{
    ESP_ERROR_CHECK(web_ui_config_init());
    ESP_ERROR_CHECK(opp_clock_sync_init());
    esp_err_t sht45_error = opp_sht45_monitor_start(web_ui_config_sht45_enabled());
    if (sht45_error != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start SHT45 monitor: %s", esp_err_to_name(sht45_error));
    }
    ESP_ERROR_CHECK(web_ui_start());
    ESP_LOGI(TAG, "Open Plant Pulse firmware %s starting",
             esp_app_get_description()->version);
}