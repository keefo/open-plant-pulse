#include "sensor_protocol.h"

uint16_t opp_modbus_crc16(const uint8_t *data, size_t length)
{
    uint16_t crc = 0xffff;

    for (size_t position = 0; position < length; ++position) {
        crc ^= data[position];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc & 1U) ? (crc >> 1U) ^ 0xa001U : crc >> 1U;
        }
    }

    return crc;
}

void opp_modbus_build_read_request(uint8_t address,
                                   uint16_t first_register,
                                   uint16_t register_count,
                                   uint8_t request[8])
{
    request[0] = address;
    request[1] = 0x03;
    request[2] = (uint8_t)(first_register >> 8U);
    request[3] = (uint8_t)first_register;
    request[4] = (uint8_t)(register_count >> 8U);
    request[5] = (uint8_t)register_count;

    const uint16_t crc = opp_modbus_crc16(request, 6);
    request[6] = (uint8_t)crc;
    request[7] = (uint8_t)(crc >> 8U);
}

bool opp_modbus_parse_three_registers(const uint8_t *response,
                                      size_t length,
                                      uint8_t expected_address,
                                      opp_sensor_reading_t *reading)
{
    if (response == NULL || reading == NULL || length != 11 ||
        response[0] != expected_address || response[1] != 0x03 ||
        response[2] != 6) {
        return false;
    }

    const uint16_t expected_crc = opp_modbus_crc16(response, length - 2);
    const uint16_t received_crc = (uint16_t)response[9] |
                                  ((uint16_t)response[10] << 8U);
    if (expected_crc != received_crc) {
        return false;
    }

    reading->moisture_tenths_percent = ((uint16_t)response[3] << 8U) | response[4];
    reading->temperature_tenths_celsius =
        (int16_t)(((uint16_t)response[5] << 8U) | response[6]);
    reading->conductivity_us_cm = ((uint16_t)response[7] << 8U) | response[8];
    return true;
}