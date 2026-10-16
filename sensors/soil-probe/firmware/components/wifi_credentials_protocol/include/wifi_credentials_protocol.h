#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Household Wi-Fi credentials, delivered hub to sensor.
 *
 * A payload of its own rather than an extension of the device configuration,
 * for two reasons. The configuration payload already occupies 171 of the roughly
 * 253 bytes an ATT write can carry at the negotiated MTU, and adding a network
 * name and password to it would overflow. And Wi-Fi is optional: a sensor works
 * fully without it, so it should not ride along with settings that are not.
 *
 * It shares the one established characteristic. The first byte is the protocol
 * version, which is what tells the three payloads apart: 1 is a forced-report
 * acknowledgement, 2 is device configuration, 3 is this. Reusing the
 * characteristic avoids a GATT service-cache migration on upgrade. */

#define OPP_WIFI_CREDENTIALS_PROTOCOL_VERSION 3
#define OPP_WIFI_SSID_MAX_BYTES 32
#define OPP_WIFI_PASSWORD_MAX_BYTES 63
#define OPP_WIFI_CREDENTIALS_PAYLOAD_MAX_SIZE 103

typedef struct {
    uint32_t revision;
    bool enabled;
    char ssid[OPP_WIFI_SSID_MAX_BYTES + 1];
    char password[OPP_WIFI_PASSWORD_MAX_BYTES + 1];
} opp_wifi_credentials_t;

/* Encode credentials for the wire. Returns the byte count, or 0 if the input is
 * not something that can be represented. */
size_t opp_wifi_credentials_encode(
    const opp_wifi_credentials_t *credentials,
    uint8_t output[OPP_WIFI_CREDENTIALS_PAYLOAD_MAX_SIZE]);

/* Decode a payload. Returns false for anything malformed, over-long, or not
 * this protocol version, leaving the output untouched. */
bool opp_wifi_credentials_decode(const uint8_t *payload, size_t payload_size,
                                 opp_wifi_credentials_t *credentials);

/* True when a payload claims to be this protocol, so a reader can tell the three
 * payload kinds apart before attempting to decode. */
bool opp_wifi_credentials_matches(const uint8_t *payload, size_t payload_size);
