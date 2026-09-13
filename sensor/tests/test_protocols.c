#include <assert.h>
#include <stdint.h>
#include <string.h>

#include "bthome_payload.h"
#include "sensor_protocol.h"

static void test_modbus_request(void)
{
    uint8_t request[8];
    const uint8_t expected[] = {0x01, 0x03, 0x00, 0x00, 0x00, 0x03, 0x05, 0xcb};

    opp_modbus_build_read_request(1, 0, 3, request);
    assert(memcmp(request, expected, sizeof(expected)) == 0);
}

static void test_modbus_response(void)
{
    uint8_t response[] = {0x01, 0x03, 0x06, 0x01, 0x41, 0x00, 0xeb, 0x04, 0xd2, 0, 0};
    const uint16_t crc = opp_modbus_crc16(response, 9);
    response[9] = (uint8_t)crc;
    response[10] = (uint8_t)(crc >> 8U);

    opp_sensor_reading_t reading;
    assert(opp_modbus_parse_three_registers(response, sizeof(response), 1, &reading));
    assert(reading.moisture_tenths_percent == 321);
    assert(reading.temperature_tenths_celsius == 235);
    assert(reading.conductivity_us_cm == 1234);

    response[10] ^= 0xff;
    assert(!opp_modbus_parse_three_registers(response, sizeof(response), 1, &reading));
}

static void test_bthome_payload(void)
{
    const opp_sensor_reading_t reading = {
        .moisture_tenths_percent = 321,
        .temperature_tenths_celsius = 235,
        .conductivity_us_cm = 1234,
    };
    const uint8_t expected[] = {0x40, 0x02, 0x2e, 0x09, 0x14,
                                0x8a, 0x0c, 0x56, 0xd2, 0x04};
    uint8_t payload[OPP_BTHOME_SERVICE_DATA_SIZE];

    assert(opp_bthome_encode_service_data(&reading, payload) == sizeof(payload));
    assert(memcmp(payload, expected, sizeof(expected)) == 0);
}

int main(void)
{
    test_modbus_request();
    test_modbus_response();
    test_bthome_payload();
    return 0;
}