#include <assert.h>
#include <stdint.h>
#include <string.h>

#include "bthome_payload.h"
#include "clock_policy.h"
#include "power_source.h"
#include "sensor_protocol.h"
#include "sht45_decode.h"

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

static void test_sht45_response(void)
{
    const uint8_t crc_fixture[] = {0xbe, 0xef};
    const uint8_t response[] = {0x66, 0x66, 0x93, 0x80, 0x00, 0xa2};
    opp_sht45_sample_t sample;

    assert(opp_sht45_crc8(crc_fixture, sizeof(crc_fixture)) == 0x92);
    assert(opp_sht45_crc8(response, 2) == 0x93);
    assert(opp_sht45_decode_response(response, &sample));
    assert(sample.air_temperature_c > 24.99f && sample.air_temperature_c < 25.01f);
    assert(sample.air_humidity_percent > 56.49f && sample.air_humidity_percent < 56.51f);

    uint8_t corrupted_response[sizeof(response)];
    memcpy(corrupted_response, response, sizeof(response));
    corrupted_response[5] ^= 0xff;
    assert(!opp_sht45_decode_response(corrupted_response, &sample));
}

static void test_sht45_humidity_clamping(void)
{
    const uint8_t dry_response[] = {0x66, 0x66, 0x93, 0x00, 0x00, 0x81};
    const uint8_t wet_response[] = {0x66, 0x66, 0x93, 0xff, 0xff, 0xac};
    opp_sht45_sample_t sample;

    assert(opp_sht45_decode_response(dry_response, &sample));
    assert(sample.air_humidity_percent == 0.0f);
    assert(opp_sht45_decode_response(wet_response, &sample));
    assert(sample.air_humidity_percent == 100.0f);
}

static void test_clock_policy(void)
{
    assert(!opp_clock_epoch_is_plausible(OPP_CLOCK_EARLIEST_UNIX_SECONDS - 1));
    assert(opp_clock_epoch_is_plausible(OPP_CLOCK_EARLIEST_UNIX_SECONDS));
    assert(!opp_clock_epoch_is_plausible(OPP_CLOCK_LATEST_UNIX_SECONDS));

    const int64_t last_sync = OPP_CLOCK_EARLIEST_UNIX_SECONDS + 100;
    assert(!opp_clock_resync_is_due(last_sync + 43199, last_sync, 43200));
    assert(opp_clock_resync_is_due(last_sync + 43200, last_sync, 43200));
    assert(opp_clock_resync_is_due(last_sync - 1, last_sync, 43200));
    assert(opp_clock_resync_is_due(last_sync, 0, 43200));

    assert(opp_clock_adjustment_ms(100000, 250000, 100300) == 50);
    assert(opp_clock_adjustment_ms(100000, 250000, 100100) == -150);
}

static void test_power_source_policy(void)
{
    assert(strcmp(opp_power_source_status_value(true), "usb") == 0);
    assert(strcmp(opp_power_source_status_value(false), "battery_inferred") == 0);
}

int main(void)
{
    test_modbus_request();
    test_modbus_response();
    test_bthome_payload();
    test_sht45_response();
    test_sht45_humidity_clamping();
    test_clock_policy();
    test_power_source_policy();
    return 0;
}