#pragma once

#include <stdbool.h>

#include "esp_err.h"

esp_err_t web_ui_start(void);

/* Whether this sensor is on the household network, and the address it was
 * given. The hub cannot see either over Bluetooth, so the sensor reports them
 * when the hub reads its configuration back; without that, a console that is
 * switched on stays "waiting" for ever. */
bool web_ui_station_joined(void);
const char *web_ui_station_address(void);

/* Take the sensor off the network and stop serving the console.
 *
 * Releasing a sensor has to reach the radio, not just the records: a sensor
 * nobody owns must not still be answering on the household network. */
void web_ui_stop(void);

/* Bring the console back after web_ui_stop(), reusing what was already set up. */
esp_err_t web_ui_resume(void);
