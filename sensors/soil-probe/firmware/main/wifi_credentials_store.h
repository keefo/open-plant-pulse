#pragma once

#include "esp_err.h"
#include "wifi_credentials_protocol.h"

/* The household network, as delivered by the hub over the bonded link.
 *
 * Stored beside the device configuration and cleared by the same reset, because
 * both belong to the hub that owns this sensor. The password is write-only from
 * the outside: it is never returned on a read, never placed in a status
 * response, and never logged. */

esp_err_t opp_wifi_credentials_store_init(void);

/* Copy the stored credentials out. Revision 0 means none have arrived. */
void opp_wifi_credentials_store_get(opp_wifi_credentials_t *credentials);

/* Apply a delivered payload. Older or equal revisions are ignored, which makes
 * repeated delivery idempotent. */
esp_err_t opp_wifi_credentials_store_apply(const opp_wifi_credentials_t *credentials);

/* Forget the network. Part of returning the sensor to onboarding. */
esp_err_t opp_wifi_credentials_store_clear(void);
