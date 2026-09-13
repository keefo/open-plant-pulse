#include "esp_app_desc.h"
#include "esp_log.h"
#include "web_ui.h"

static const char *TAG = "plant_pulse";

void app_main(void)
{
    ESP_ERROR_CHECK(web_ui_start());
    ESP_LOGI(TAG, "Open Plant Pulse firmware %s starting",
             esp_app_get_description()->version);
}