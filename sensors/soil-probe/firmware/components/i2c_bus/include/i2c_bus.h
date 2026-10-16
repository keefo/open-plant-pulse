#pragma once

#include "driver/i2c_master.h"
#include "esp_err.h"

/* The one I2C bus the SHT45 and INA219 share (XIAO D1/D3 on the assembled node).
 *
 * The ESP32-C3 has a single I2C controller, so every I2C device lives on this
 * bus. It is created by the first acquire and deleted by the last release, so
 * switching one sensor off does not pull the bus out from under another. */
esp_err_t opp_i2c_bus_acquire(i2c_master_bus_handle_t *bus);
esp_err_t opp_i2c_bus_release(void);
