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

static bool is_read_function(uint8_t function)
{
    return function == 0x03 || function == 0x04;
}

bool opp_modbus_build_read(uint8_t address,
                           uint8_t function,
                           uint16_t first_register,
                           uint16_t register_count,
                           uint8_t request[8])
{
    if (request == NULL || !is_read_function(function) || register_count == 0 ||
        register_count > OPP_MODBUS_MAX_READ_REGISTERS) {
        return false;
    }
    opp_modbus_build_read_request(address, first_register, register_count, request);
    request[1] = function;
    const uint16_t crc = opp_modbus_crc16(request, 6);
    request[6] = (uint8_t)crc;
    request[7] = (uint8_t)(crc >> 8U);
    return true;
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

static bool is_valid_response(const uint8_t *response,
                              size_t length,
                              uint8_t expected_address,
                              uint8_t function,
                              uint16_t register_count)
{
    const size_t data_size = 2U * (size_t)register_count;
    if (response == NULL || length != 3U + data_size + 2U ||
        response[0] != expected_address || response[1] != function ||
        response[2] != data_size) {
        return false;
    }

    const uint16_t expected_crc = opp_modbus_crc16(response, length - 2);
    const uint16_t received_crc = (uint16_t)response[length - 2] |
                                  ((uint16_t)response[length - 1] << 8U);
    return expected_crc == received_crc;
}

static bool is_valid_read_response(const uint8_t *response,
                                   size_t length,
                                   uint8_t expected_address,
                                   uint16_t register_count)
{
    return is_valid_response(response, length, expected_address, 0x03, register_count);
}

static uint16_t register_value(const uint8_t *response, size_t index)
{
    return ((uint16_t)response[3 + 2 * index] << 8U) | response[4 + 2 * index];
}

bool opp_modbus_parse_registers(const uint8_t *response,
                                size_t length,
                                uint8_t expected_address,
                                uint8_t function,
                                uint16_t register_count,
                                uint16_t *registers)
{
    if (registers == NULL || !is_read_function(function) || register_count == 0 ||
        register_count > OPP_MODBUS_MAX_READ_REGISTERS ||
        !is_valid_response(response, length, expected_address, function,
                           register_count)) {
        return false;
    }
    for (uint16_t index = 0; index < register_count; ++index) {
        registers[index] = register_value(response, index);
    }
    return true;
}

bool opp_modbus_parse_three_registers(const uint8_t *response,
                                      size_t length,
                                      uint8_t expected_address,
                                      opp_sensor_reading_t *reading)
{
    if (reading == NULL ||
        !is_valid_read_response(response, length, expected_address, 3)) {
        return false;
    }

    reading->moisture_tenths_percent = register_value(response, 0);
    reading->temperature_tenths_celsius = (int16_t)register_value(response, 1);
    reading->conductivity_us_cm = register_value(response, 2);
    return true;
}

bool opp_modbus_parse_soil_probe_response(const uint8_t *response,
                                          size_t length,
                                          uint8_t expected_address,
                                          opp_soil_probe_reading_t *reading)
{
    if (reading == NULL ||
        !is_valid_read_response(response, length, expected_address,
                                OPP_SOIL_PROBE_REGISTER_COUNT)) {
        return false;
    }

    const opp_soil_probe_reading_t parsed = {
        .moisture_tenths_percent = register_value(response, 0),
        .temperature_tenths_celsius = (int16_t)register_value(response, 1),
        .conductivity_us_cm = register_value(response, 2),
        .ph_tenths = register_value(response, 3),
        .nitrogen_mg_kg = register_value(response, 4),
        .phosphorus_mg_kg = register_value(response, 5),
        .potassium_mg_kg = register_value(response, 6),
    };
    /* Measuring ranges from the manual: 0-100 %, -40-80 C, 0-20000 uS/cm. */
    if (parsed.moisture_tenths_percent > 1000U ||
        parsed.temperature_tenths_celsius < -400 ||
        parsed.temperature_tenths_celsius > 800 ||
        parsed.conductivity_us_cm > 20000U) {
        return false;
    }
    *reading = parsed;
    return true;
}
