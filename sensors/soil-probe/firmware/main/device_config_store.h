#pragma once

#include "device_config_protocol.h"
#include "esp_err.h"

esp_err_t opp_device_config_store_init(void);
void opp_device_config_store_get(opp_device_config_t *config);
esp_err_t opp_device_config_store_apply(const opp_device_config_t *config);

/* Forget the plant, room and interval. Part of being released by a hub. */
esp_err_t opp_device_config_store_clear(void);