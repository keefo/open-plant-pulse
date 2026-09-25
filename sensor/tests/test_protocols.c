#include <assert.h>
#include <stdint.h>
#include <string.h>

#include "bthome_payload.h"
#include "clock_policy.h"
#include "device_config_protocol.h"
#include "firmware_update_protocol.h"
#include "power_source.h"
#include "report_ack_protocol.h"
#include "wifi_credentials_protocol.h"
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

static void test_bthome_v1_payload(void)
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

static void test_bthome_v2_payload(void)
{
    const opp_bthome_sample_t sample = {
        .packet_id = 42,
        .soil_available = true,
        .soil = {
            .moisture_tenths_percent = 321,
            .temperature_tenths_celsius = 235,
            .conductivity_us_cm = 1234,
        },
        .air_available = true,
        .air_temperature_tenths_celsius = 242,
        .air_humidity_hundredths_percent = 5360,
    };
    const uint8_t expected[] = {
        0x40, 0x00, 0x2a, 0x02, 0x2e, 0x09, 0x03, 0xf0, 0x14,
        0x14, 0x8a, 0x0c, 0x45, 0xf2, 0x00, 0x56, 0xd2, 0x04,
    };
    uint8_t payload[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE];

    assert(opp_bthome_encode_v2_service_data(&sample, payload) == sizeof(expected));
    assert(memcmp(payload, expected, sizeof(expected)) == 0);

    const opp_bthome_sample_t air_only = {
        .packet_id = 7,
        .air_available = true,
        .air_temperature_tenths_celsius = 241,
        .air_humidity_hundredths_percent = 5500,
    };
    const uint8_t expected_air_only[] = {
        0x40, 0x00, 0x07, 0x03, 0x7c, 0x15, 0x45, 0xf1, 0x00,
    };
    assert(opp_bthome_encode_v2_service_data(&air_only, payload) ==
           sizeof(expected_air_only));
    assert(memcmp(payload, expected_air_only, sizeof(expected_air_only)) == 0);

    opp_bthome_sample_t forced = air_only;
    forced.button_event = true;
    const uint8_t expected_forced[] = {
        0x40, 0x00, 0x07, 0x03, 0x7c, 0x15, 0x3a, 0x01, 0x45, 0xf1, 0x00,
    };
    assert(opp_bthome_encode_v2_service_data(&forced, payload) == sizeof(expected_forced));
    assert(memcmp(payload, expected_forced, sizeof(expected_forced)) == 0);
}

static void test_bthome_identity(void)
{
    char local_name[OPP_BTHOME_LOCAL_NAME_SIZE];
    assert(opp_bthome_format_local_name(UINT64_C(0xaabbccddeeff), local_name));
    assert(strcmp(local_name, "sensor-aabbccddeeff") == 0);
    assert(!opp_bthome_format_local_name(UINT64_C(0x1000000000000), local_name));
}

static void test_device_configuration(void)
{
    const opp_device_config_t config = {
        .revision = 42,
        .reporting_interval_seconds = 60,
        .plant_name = "Kitchen basil",
        .room = "Kitchen",
        .console_enabled = true,
    };
    const uint8_t expected[] = {
        0x06, 0x2a, 0x00, 0x00, 0x00, 0x3c, 0x00, 0x00, 0x00, 0x0d, 0x07, 0x01,
        'K', 'i', 't', 'c', 'h', 'e', 'n', ' ', 'b', 'a', 's', 'i', 'l',
        'K', 'i', 't', 'c', 'h', 'e', 'n',
    };
    uint8_t payload[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE];
    opp_device_config_t decoded;

    assert(opp_device_config_encode(&config, payload) == sizeof(expected));
    assert(memcmp(payload, expected, sizeof(expected)) == 0);
    assert(opp_device_config_decode(payload, sizeof(expected), &decoded));
    assert(opp_device_config_equal(&config, &decoded));

    /* A console the hub switched off must survive the round trip as off. */
    opp_device_config_t without_console = config;
    without_console.console_enabled = false;
    uint8_t off_payload[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE];
    const size_t off_size = opp_device_config_encode(&without_console, off_payload);
    assert(off_size == sizeof(expected));
    assert(off_payload[11] == 0x00);
    assert(opp_device_config_decode(off_payload, off_size, &decoded));
    assert(!decoded.console_enabled);

    payload[0] = 1;
    assert(!opp_device_config_decode(payload, sizeof(expected), &decoded));
    assert(!opp_device_config_decode(expected, sizeof(expected) - 1, &decoded));

    opp_device_config_t invalid = config;
    invalid.reporting_interval_seconds = 0;
    assert(opp_device_config_encode(&invalid, payload) == 0);
    invalid.reporting_interval_seconds = 86401;
    assert(opp_device_config_encode(&invalid, payload) == 0);

    const uint32_t requested_intervals[] = {1, 3, 5, 10, 30, 60};
    for (size_t index = 0; index < sizeof(requested_intervals) / sizeof(requested_intervals[0]);
         index++) {
        opp_device_config_t requested = config;
        requested.reporting_interval_seconds = requested_intervals[index];
        assert(opp_device_config_encode(&requested, payload) == sizeof(expected));
    }
    invalid = config;
    memset(invalid.plant_name, 'a', sizeof(invalid.plant_name));
    assert(opp_device_config_encode(&invalid, payload) == 0);
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

static void test_report_acknowledgement(void)
{
    const opp_report_ack_t expected = {
        .request_id = UINT32_C(0x12345678),
        .packet_id = 42,
    };
    const uint8_t expected_payload[] = {0x01, 0x78, 0x56, 0x34, 0x12, 0x2a};
    uint8_t payload[OPP_REPORT_ACK_PAYLOAD_SIZE];
    opp_report_ack_t decoded;

    assert(opp_report_ack_encode(&expected, payload) == sizeof(expected_payload));
    assert(memcmp(payload, expected_payload, sizeof(expected_payload)) == 0);
    assert(opp_report_ack_decode(payload, sizeof(payload), &decoded));
    assert(opp_report_ack_equal(&decoded, &expected));

    payload[0] = 2;
    assert(!opp_report_ack_decode(payload, sizeof(payload), &decoded));
    payload[0] = OPP_REPORT_ACK_PROTOCOL_VERSION;
    memset(payload + 1, 0, 4);
    assert(!opp_report_ack_decode(payload, sizeof(payload), &decoded));
    assert(!opp_report_ack_decode(expected_payload, sizeof(expected_payload) - 1, &decoded));
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

static void test_bthome_onboarding_beacon(void)
{
    uint8_t payload[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE];
    const size_t size = opp_bthome_encode_v2_beacon(9, payload);
    assert(size == 3);
    assert(payload[0] == 0x40);
    assert(payload[1] == 0x00);
    assert(payload[2] == 9);

    /* A beacon says only that the sensor exists. A measurement payload for the
     * same sensor stays longer, so the two can never be confused. */
    opp_bthome_sample_t sample = {.packet_id = 9, .air_available = true,
                                  .air_temperature_tenths_celsius = 214,
                                  .air_humidity_hundredths_percent = 4800};
    uint8_t measured[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE];
    assert(opp_bthome_encode_v2_service_data(&sample, measured) > size);
}

static void test_wifi_credentials(void)
{
    opp_wifi_credentials_t sent = {
        .revision = 7,
        .enabled = true,
    };
    strcpy(sent.ssid, "BEYONDCOW-2.4G");
    strcpy(sent.password, "softmoss");

    uint8_t payload[OPP_WIFI_CREDENTIALS_PAYLOAD_MAX_SIZE];
    const size_t size = opp_wifi_credentials_encode(&sent, payload);
    assert(size == 1 + 4 + 1 + 1 + 14 + 1 + 8);
    assert(payload[0] == OPP_WIFI_CREDENTIALS_PROTOCOL_VERSION);

    /* The three payloads on the shared characteristic stay distinguishable. */
    assert(opp_wifi_credentials_matches(payload, size));
    uint8_t configuration[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE];
    const opp_device_config_t device = {
        .revision = 1, .reporting_interval_seconds = 1800,
        .plant_name = "white bird", .room = "living",
    };
    const size_t configuration_size = opp_device_config_encode(&device, configuration);
    assert(!opp_wifi_credentials_matches(configuration, configuration_size));

    opp_wifi_credentials_t received;
    assert(opp_wifi_credentials_decode(payload, size, &received));
    assert(received.revision == 7);
    assert(received.enabled);
    assert(strcmp(received.ssid, "BEYONDCOW-2.4G") == 0);
    assert(strcmp(received.password, "softmoss") == 0);

    /* Switching the console off keeps the network, so switching it back on needs
     * nothing resent. */
    opp_wifi_credentials_t disabled = sent;
    disabled.enabled = false;
    const size_t disabled_size = opp_wifi_credentials_encode(&disabled, payload);
    assert(disabled_size == size);
    assert(opp_wifi_credentials_decode(payload, disabled_size, &received));
    assert(!received.enabled);
    assert(strcmp(received.ssid, "BEYONDCOW-2.4G") == 0);

    /* An open network is representable; a switched-on console without one is not. */
    opp_wifi_credentials_t open_network = {.revision = 1, .enabled = true};
    strcpy(open_network.ssid, "guest");
    assert(opp_wifi_credentials_encode(&open_network, payload) > 0);
    opp_wifi_credentials_t nameless = {.revision = 1, .enabled = true};
    assert(opp_wifi_credentials_encode(&nameless, payload) == 0);

    /* Longest permitted values still fit the payload budget. */
    opp_wifi_credentials_t longest = {.revision = 0xffffffffU, .enabled = true};
    memset(longest.ssid, 'S', OPP_WIFI_SSID_MAX_BYTES);
    memset(longest.password, 'P', OPP_WIFI_PASSWORD_MAX_BYTES);
    const size_t longest_size = opp_wifi_credentials_encode(&longest, payload);
    assert(longest_size == OPP_WIFI_CREDENTIALS_PAYLOAD_MAX_SIZE);
    assert(opp_wifi_credentials_decode(payload, longest_size, &received));
    assert(strlen(received.ssid) == OPP_WIFI_SSID_MAX_BYTES);
    assert(strlen(received.password) == OPP_WIFI_PASSWORD_MAX_BYTES);

    /* Malformed input is refused rather than half-applied. */
    assert(!opp_wifi_credentials_decode(payload, longest_size - 1, &received));
    assert(!opp_wifi_credentials_decode(payload, 0, &received));
    uint8_t wrong_version[8] = {2, 0, 0, 0, 0, 0, 0, 0};
    assert(!opp_wifi_credentials_decode(wrong_version, sizeof(wrong_version), &received));
    uint8_t lying_length[9] = {3, 1, 0, 0, 0, 1, 40, 'a', 0};
    assert(!opp_wifi_credentials_decode(lying_length, sizeof(lying_length), &received));
}


static void test_firmware_update_command(void)
{
    opp_firmware_update_command_t command = {
        .update_id = 7,
        .size_bytes = 1294784,
        .address = {192, 168, 0, 231},
        .port = 8081,
    };
    for (size_t index = 0; index < sizeof(command.digest); ++index) {
        command.digest[index] = (uint8_t)(0xab);
    }

    uint8_t payload[OPP_FIRMWARE_UPDATE_PAYLOAD_SIZE];
    assert(opp_firmware_update_encode(&command, payload) == sizeof(payload));
    assert(payload[0] == OPP_FIRMWARE_UPDATE_PROTOCOL_VERSION);

    opp_firmware_update_command_t decoded;
    assert(opp_firmware_update_decode(payload, sizeof(payload), &decoded));
    assert(decoded.update_id == command.update_id);
    assert(decoded.size_bytes == command.size_bytes);
    assert(decoded.port == command.port);
    assert(memcmp(decoded.address, command.address, sizeof(command.address)) == 0);
    assert(memcmp(decoded.digest, command.digest, sizeof(command.digest)) == 0);

    /* One characteristic carries several payloads. Each reader must recognise
     * only its own, or a payload would be applied as the wrong thing. */
    uint8_t configuration[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE];
    const opp_device_config_t config = {
        .revision = 3,
        .reporting_interval_seconds = 60,
        .plant_name = "Fern",
        .room = "Office",
    };
    const size_t configuration_size = opp_device_config_encode(&config, configuration);
    assert(configuration_size > 0);
    assert(!opp_firmware_update_decode(configuration, configuration_size, &decoded));
    opp_device_config_t misread;
    assert(!opp_device_config_decode(payload, sizeof(payload), &misread));

    /* A command that cannot be acted on is refused rather than half-applied. */
    uint8_t broken[OPP_FIRMWARE_UPDATE_PAYLOAD_SIZE];
    memcpy(broken, payload, sizeof(broken));
    broken[1] = 0;
    broken[2] = 0;
    broken[3] = 0;
    broken[4] = 0;
    assert(!opp_firmware_update_decode(broken, sizeof(broken), &decoded));
    assert(!opp_firmware_update_decode(payload, sizeof(payload) - 1, &decoded));
}

static void test_firmware_update_url(void)
{
    opp_firmware_update_command_t command = {
        .update_id = 1,
        .size_bytes = 1024,
        .address = {192, 168, 0, 231},
        .port = 8081,
    };
    for (size_t index = 0; index < sizeof(command.digest); ++index) {
        command.digest[index] = (uint8_t)index;
    }

    char url[128];
    const size_t written = opp_firmware_update_format_url(&command, url, sizeof(url));
    assert(written > 0);
    assert(strcmp(url,
                  "http://192.168.0.231:8081/firmware/"
                  "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f.bin") == 0);

    char cramped[32];
    assert(opp_firmware_update_format_url(&command, cramped, sizeof(cramped)) == 0);
}

static void test_firmware_status_suffix(void)
{
    uint8_t status[OPP_FIRMWARE_STATUS_SIZE];
    assert(opp_firmware_status_encode(OPP_FIRMWARE_STATE_DOWNLOADING, 41,
                                      OPP_FIRMWARE_FAILURE_NONE, status) == sizeof(status));
    assert(status[0] == OPP_FIRMWARE_STATUS_MARKER);
    assert(status[1] == OPP_FIRMWARE_STATE_DOWNLOADING);
    assert(status[2] == 41);
    assert(status[3] == OPP_FIRMWARE_FAILURE_NONE);

    /* A percentage beyond the end of the download says nothing useful. */
    assert(opp_firmware_status_encode(OPP_FIRMWARE_STATE_INSTALLING, 240,
                                      OPP_FIRMWARE_FAILURE_NONE, status) == sizeof(status));
    assert(status[2] == 100);
}

int main(void)
{
    test_modbus_request();
    test_modbus_response();
    test_bthome_v1_payload();
    test_bthome_v2_payload();
    test_bthome_identity();
    test_device_configuration();
    test_report_acknowledgement();
    test_sht45_response();
    test_sht45_humidity_clamping();
    test_clock_policy();
    test_power_source_policy();
    test_bthome_onboarding_beacon();
    test_wifi_credentials();
    test_firmware_update_command();
    test_firmware_update_url();
    test_firmware_status_suffix();
    return 0;
}