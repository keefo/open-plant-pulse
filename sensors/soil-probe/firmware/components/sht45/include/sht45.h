#pragma once

#include "esp_err.h"
#include "sht45_types.h"

esp_err_t opp_sht45_init(void);
esp_err_t opp_sht45_deinit(void);
esp_err_t opp_sht45_probe(void);
esp_err_t opp_sht45_read(opp_sht45_sample_t *sample);