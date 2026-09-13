#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint16_t moisture_tenths_percent;
    int16_t temperature_tenths_celsius;
    uint16_t conductivity_us_cm;
} opp_sensor_reading_t;

uint16_t opp_modbus_crc16(const uint8_t *data, size_t length);

void opp_modbus_build_read_request(uint8_t address,
                                   uint16_t first_register,
                                   uint16_t register_count,
                                   uint8_t request[8]);

bool opp_modbus_parse_three_registers(const uint8_t *response,
                                      size_t length,
                                      uint8_t expected_address,
                                      opp_sensor_reading_t *reading);