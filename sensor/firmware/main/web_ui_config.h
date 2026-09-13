#pragma once

#include <stddef.h>

#include "esp_err.h"

#define WEB_UI_CONFIG_JSON_MAX 1536

esp_err_t web_ui_config_load(void);
esp_err_t web_ui_config_read(char *output, size_t output_size);
esp_err_t web_ui_config_write(const char *json, size_t length,
                              char *error_message, size_t error_message_size);
esp_err_t web_ui_config_reset(void);