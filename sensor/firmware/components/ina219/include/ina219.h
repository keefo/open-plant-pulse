#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    int32_t bus_millivolts;
    int32_t shunt_microvolts;
    bool overflow;
} opp_ina219_sample_t;

/* Join the shared I2C bus, find the INA219 and start continuous conversion. */
esp_err_t opp_ina219_init(uint8_t address);
esp_err_t opp_ina219_read(opp_ina219_sample_t *sample);
/* Stop converting (about 6 uA) and leave the bus. */
esp_err_t opp_ina219_deinit(void);
