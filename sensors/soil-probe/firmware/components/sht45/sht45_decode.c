#include "sht45_decode.h"

uint8_t opp_sht45_crc8(const uint8_t *data, size_t length)
{
    uint8_t crc = 0xff;

    for (size_t index = 0; index < length; index++) {
        crc ^= data[index];
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc & 0x80U) != 0U ? (uint8_t)((crc << 1U) ^ 0x31U)
                                      : (uint8_t)(crc << 1U);
        }
    }
    return crc;
}

bool opp_sht45_decode_response(const uint8_t response[OPP_SHT45_RESPONSE_SIZE],
                               opp_sht45_sample_t *sample)
{
    if (response == NULL || sample == NULL ||
        opp_sht45_crc8(response, 2) != response[2] ||
        opp_sht45_crc8(&response[3], 2) != response[5]) {
        return false;
    }

    const uint16_t raw_temperature = ((uint16_t)response[0] << 8U) | response[1];
    const uint16_t raw_humidity = ((uint16_t)response[3] << 8U) | response[4];
    const float air_temperature_c = -45.0f + 175.0f * raw_temperature / 65535.0f;
    float air_humidity_percent = -6.0f + 125.0f * raw_humidity / 65535.0f;

    if (air_humidity_percent < 0.0f) {
        air_humidity_percent = 0.0f;
    } else if (air_humidity_percent > 100.0f) {
        air_humidity_percent = 100.0f;
    }

    sample->air_temperature_c = air_temperature_c;
    sample->air_humidity_percent = air_humidity_percent;
    return true;
}