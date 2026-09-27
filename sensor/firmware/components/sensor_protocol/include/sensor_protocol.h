#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint16_t moisture_tenths_percent;
    int16_t temperature_tenths_celsius;
    uint16_t conductivity_us_cm;
} opp_sensor_reading_t;

/* The seven measurement registers of a ComWinTop NPK-type soil probe
 * (NPKPHCTH-S), read with one function 0x03 request from register 0x0000.
 * Register order and scaling follow the CWT "NPK type" manual V1.4. */
#define OPP_SOIL_PROBE_FIRST_REGISTER 0x0000U
#define OPP_SOIL_PROBE_REGISTER_COUNT 7U
/* Address, function, byte count, seven registers, CRC. */
#define OPP_SOIL_PROBE_RESPONSE_SIZE (3U + 2U * OPP_SOIL_PROBE_REGISTER_COUNT + 2U)

typedef struct {
    uint16_t moisture_tenths_percent;
    int16_t temperature_tenths_celsius;
    uint16_t conductivity_us_cm;
    uint16_t ph_tenths;
    uint16_t nitrogen_mg_kg;
    uint16_t phosphorus_mg_kg;
    uint16_t potassium_mg_kg;
} opp_soil_probe_reading_t;

uint16_t opp_modbus_crc16(const uint8_t *data, size_t length);

void opp_modbus_build_read_request(uint8_t address,
                                   uint16_t first_register,
                                   uint16_t register_count,
                                   uint8_t request[8]);

/* A read with either register-reading function: 0x03 (holding registers) or
 * 0x04 (input registers). Probes differ in which one carries their data. */
#define OPP_MODBUS_MAX_READ_REGISTERS 32U

bool opp_modbus_build_read(uint8_t address,
                           uint8_t function,
                           uint16_t first_register,
                           uint16_t register_count,
                           uint8_t request[8]);

/* Validate a complete read reply and copy out its register values. */
bool opp_modbus_parse_registers(const uint8_t *response,
                                size_t length,
                                uint8_t expected_address,
                                uint8_t function,
                                uint16_t register_count,
                                uint16_t *registers);

bool opp_modbus_parse_three_registers(const uint8_t *response,
                                      size_t length,
                                      uint8_t expected_address,
                                      opp_sensor_reading_t *reading);

/* Parse a complete seven-register probe response.
 *
 * Rejects a wrong address, function, length or CRC, and a moisture,
 * temperature or conductivity outside the probe's documented measuring range:
 * a frame that passes its CRC but reports 6553.5 % moisture is still not a
 * measurement, and passing it on would make the whole report unencodable. */
bool opp_modbus_parse_soil_probe_response(const uint8_t *response,
                                          size_t length,
                                          uint8_t expected_address,
                                          opp_soil_probe_reading_t *reading);
