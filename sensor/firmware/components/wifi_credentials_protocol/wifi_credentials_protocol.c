#include "wifi_credentials_protocol.h"

#include <string.h>

/* Layout, little-endian:
 *   0      version (3)
 *   1..4   revision
 *   5      enabled
 *   6      ssid length
 *   7..    ssid
 *   +1     password length
 *   +      password
 *
 * A disabled console still carries its network, so switching it back on does not
 * need the hub to resend anything. */

static bool text_is_printable(const char *text, size_t length)
{
    for (size_t index = 0; index < length; ++index) {
        const unsigned char character = (unsigned char)text[index];
        if (character < 0x20 || character == 0x7f) {
            return false;
        }
    }
    return true;
}

size_t opp_wifi_credentials_encode(
    const opp_wifi_credentials_t *credentials,
    uint8_t output[OPP_WIFI_CREDENTIALS_PAYLOAD_MAX_SIZE])
{
    if (credentials == NULL || output == NULL) {
        return 0;
    }
    const size_t ssid_length = strnlen(credentials->ssid, OPP_WIFI_SSID_MAX_BYTES + 1);
    const size_t password_length =
        strnlen(credentials->password, OPP_WIFI_PASSWORD_MAX_BYTES + 1);
    if (ssid_length > OPP_WIFI_SSID_MAX_BYTES ||
        password_length > OPP_WIFI_PASSWORD_MAX_BYTES) {
        return 0;
    }
    /* A console cannot be switched on without a network to join. */
    if (credentials->enabled && ssid_length == 0) {
        return 0;
    }
    if (!text_is_printable(credentials->ssid, ssid_length) ||
        !text_is_printable(credentials->password, password_length)) {
        return 0;
    }

    size_t offset = 0;
    output[offset++] = OPP_WIFI_CREDENTIALS_PROTOCOL_VERSION;
    output[offset++] = (uint8_t)(credentials->revision & 0xffU);
    output[offset++] = (uint8_t)((credentials->revision >> 8) & 0xffU);
    output[offset++] = (uint8_t)((credentials->revision >> 16) & 0xffU);
    output[offset++] = (uint8_t)((credentials->revision >> 24) & 0xffU);
    output[offset++] = credentials->enabled ? 1U : 0U;
    output[offset++] = (uint8_t)ssid_length;
    memcpy(&output[offset], credentials->ssid, ssid_length);
    offset += ssid_length;
    output[offset++] = (uint8_t)password_length;
    memcpy(&output[offset], credentials->password, password_length);
    offset += password_length;
    return offset;
}

bool opp_wifi_credentials_matches(const uint8_t *payload, size_t payload_size)
{
    return payload != NULL && payload_size >= 8 &&
           payload[0] == OPP_WIFI_CREDENTIALS_PROTOCOL_VERSION;
}

bool opp_wifi_credentials_decode(const uint8_t *payload, size_t payload_size,
                                 opp_wifi_credentials_t *credentials)
{
    if (credentials == NULL || !opp_wifi_credentials_matches(payload, payload_size) ||
        payload_size > OPP_WIFI_CREDENTIALS_PAYLOAD_MAX_SIZE) {
        return false;
    }

    size_t offset = 1;
    const uint32_t revision = (uint32_t)payload[offset] |
                              ((uint32_t)payload[offset + 1] << 8) |
                              ((uint32_t)payload[offset + 2] << 16) |
                              ((uint32_t)payload[offset + 3] << 24);
    offset += 4;
    const uint8_t enabled = payload[offset++];
    if (enabled > 1U) {
        return false;
    }

    const size_t ssid_length = payload[offset++];
    if (ssid_length > OPP_WIFI_SSID_MAX_BYTES || offset + ssid_length >= payload_size) {
        return false;
    }
    const char *ssid = (const char *)&payload[offset];
    offset += ssid_length;

    const size_t password_length = payload[offset++];
    if (password_length > OPP_WIFI_PASSWORD_MAX_BYTES ||
        offset + password_length != payload_size) {
        return false;
    }
    const char *password = (const char *)&payload[offset];

    if (!text_is_printable(ssid, ssid_length) ||
        !text_is_printable(password, password_length)) {
        return false;
    }
    if (enabled == 1U && ssid_length == 0) {
        return false;
    }

    credentials->revision = revision;
    credentials->enabled = enabled == 1U;
    memcpy(credentials->ssid, ssid, ssid_length);
    credentials->ssid[ssid_length] = '\0';
    memcpy(credentials->password, password, password_length);
    credentials->password[password_length] = '\0';
    return true;
}
