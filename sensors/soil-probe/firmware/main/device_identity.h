#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/* Identity that survives a factory reset.
 *
 * The pairing passkey is generated once, on the first boot of a given device, and
 * then never changes. It is what proves the person adding this sensor is holding
 * it, so a reset must not erase it: only the bond and the configuration go. */

#define OPP_PASSKEY_MIN 0U
#define OPP_PASSKEY_MAX 999999U

/* Load the passkey, generating one on first boot. Safe to call more than once. */
esp_err_t opp_device_identity_init(void);

/* The six-digit passkey printed on this device's label. */
uint32_t opp_device_identity_passkey(void);

/* True once a hub has bonded with this sensor.
 *
 * The bond itself is the authority, but it can only be counted while the NimBLE
 * host is running, and this firmware brings the radio up only for a bounded
 * window. The answer is therefore mirrored into NVS whenever the host observes
 * it, so the diagnostics console can still say which state the sensor is in. */
bool opp_device_identity_is_onboarded(void);

/* Record what the running host observed, so the mirror stays true. */
esp_err_t opp_device_identity_set_onboarded(bool onboarded);
