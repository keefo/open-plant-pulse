#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "firmware_update_protocol.h"

/* Installing what the hub sent, and saying so while it happens.
 *
 * The hub commands over the bonded Bluetooth link and serves the image over the
 * household network. Only the fetching and writing happen here. */

/* Called once at start-up, before anything else can fail.
 *
 * A freshly installed image boots as pending: if it never reaches
 * opp_firmware_update_confirm(), the bootloader runs the previous one again at
 * the next reset. This reports which case this boot is. */
esp_err_t opp_firmware_update_init(void);

/* This image works well enough to keep. Called once the sensor has done the
 * thing it exists to do, which is to be heard by its hub. */
void opp_firmware_update_confirm(void);

/* Begin an update, or say why not. Repeating a command already running is
 * accepted and ignored, because the hub retries until it is told otherwise. */
esp_err_t opp_firmware_update_start(const opp_firmware_update_command_t *command);

/* Where the update has got to, for the hub's read-back. */
void opp_firmware_update_status(opp_firmware_state_t *state, uint8_t *percent,
                                opp_firmware_failure_t *failure);

/* Is an update running? A sensor in the middle of one must stay awake and stay
 * on the network, whatever its reporting interval says. */
bool opp_firmware_update_in_progress(void);
