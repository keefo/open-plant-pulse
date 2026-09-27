#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "battery_gauge.h"
#include "bthome_payload.h"
#include "clock_policy.h"
#include "device_config_protocol.h"
#include "firmware_update_protocol.h"
#include "ina219_decode.h"
#include "power_source.h"
#include "report_ack_protocol.h"
#include "report_queue.h"
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

/* The request and response below are the worked example in the CWT "NPK type"
 * manual V1.4, CRCs included, so they check the register map against the
 * vendor's own document rather than against this code. */
static void test_soil_probe_request(void)
{
    uint8_t request[8];
    const uint8_t expected[] = {0x01, 0x03, 0x00, 0x00, 0x00, 0x07, 0x04, 0x08};

    opp_modbus_build_read_request(1, OPP_SOIL_PROBE_FIRST_REGISTER,
                                  OPP_SOIL_PROBE_REGISTER_COUNT, request);
    assert(memcmp(request, expected, sizeof(expected)) == 0);
}

static void test_soil_probe_response(void)
{
    uint8_t response[] = {
        0x01, 0x03, 0x0e, 0x01, 0xd0, 0x01, 0x4c, 0x00, 0x2c, 0x00,
        0x5a, 0x00, 0x20, 0x00, 0x58, 0x00, 0x68, 0x70, 0x29,
    };
    assert(sizeof(response) == OPP_SOIL_PROBE_RESPONSE_SIZE);

    opp_soil_probe_reading_t reading;
    assert(opp_modbus_parse_soil_probe_response(response, sizeof(response), 1, &reading));
    assert(reading.moisture_tenths_percent == 464);
    assert(reading.temperature_tenths_celsius == 332);
    assert(reading.conductivity_us_cm == 44);
    assert(reading.ph_tenths == 90);
    assert(reading.nitrogen_mg_kg == 32);
    assert(reading.phosphorus_mg_kg == 88);
    assert(reading.potassium_mg_kg == 104);

    assert(!opp_modbus_parse_soil_probe_response(response, sizeof(response), 2, &reading));
    assert(!opp_modbus_parse_soil_probe_response(response, sizeof(response) - 1, 1, &reading));
    response[18] ^= 0xff;
    assert(!opp_modbus_parse_soil_probe_response(response, sizeof(response), 1, &reading));
}

static void test_soil_probe_negative_temperature(void)
{
    uint8_t response[] = {
        0x01, 0x03, 0x0e, 0x00, 0x00, 0xff, 0x9c, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0, 0,
    };
    const uint16_t crc = opp_modbus_crc16(response, sizeof(response) - 2);
    response[17] = (uint8_t)crc;
    response[18] = (uint8_t)(crc >> 8U);

    opp_soil_probe_reading_t reading;
    assert(opp_modbus_parse_soil_probe_response(response, sizeof(response), 1, &reading));
    assert(reading.temperature_tenths_celsius == -100);
}

static void test_soil_probe_rejects_out_of_range(void)
{
    /* 0xffff moisture with a valid CRC: a frame, but not a measurement. */
    uint8_t response[] = {
        0x01, 0x03, 0x0e, 0xff, 0xff, 0x00, 0xeb, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0, 0,
    };
    const uint16_t crc = opp_modbus_crc16(response, sizeof(response) - 2);
    response[17] = (uint8_t)crc;
    response[18] = (uint8_t)(crc >> 8U);

    opp_soil_probe_reading_t reading;
    assert(!opp_modbus_parse_soil_probe_response(response, sizeof(response), 1, &reading));
}

static void test_modbus_generic_read(void)
{
    uint8_t request[8];
    const uint8_t holding[] = {0x01, 0x03, 0x00, 0x00, 0x00, 0x07, 0x04, 0x08};
    assert(opp_modbus_build_read(1, 0x03, 0, 7, request));
    assert(memcmp(request, holding, sizeof(holding)) == 0);

    assert(opp_modbus_build_read(1, 0x04, 0, 7, request));
    assert(request[1] == 0x04);
    const uint16_t crc = opp_modbus_crc16(request, 6);
    assert(request[6] == (uint8_t)crc && request[7] == (uint8_t)(crc >> 8U));

    assert(!opp_modbus_build_read(1, 0x06, 0, 7, request));
    assert(!opp_modbus_build_read(1, 0x03, 0, 0, request));
    assert(!opp_modbus_build_read(1, 0x03, 0, OPP_MODBUS_MAX_READ_REGISTERS + 1, request));

    uint8_t response[] = {0x01, 0x04, 0x04, 0x01, 0xd0, 0xff, 0x9c, 0, 0};
    const uint16_t response_crc = opp_modbus_crc16(response, 7);
    response[7] = (uint8_t)response_crc;
    response[8] = (uint8_t)(response_crc >> 8U);
    uint16_t registers[2];
    assert(opp_modbus_parse_registers(response, sizeof(response), 1, 0x04, 2, registers));
    assert(registers[0] == 0x01d0 && registers[1] == 0xff9c);
    /* A 0x04 reply is not an answer to a 0x03 request. */
    assert(!opp_modbus_parse_registers(response, sizeof(response), 1, 0x03, 2, registers));
}

static bool near(float actual, float expected, float tolerance)
{
    return fabsf(actual - expected) <= tolerance;
}

static void test_ina219_decode(void)
{
    /* Datasheet examples: 0x1f40 is 1000 bus steps (4.000 V); the shunt
     * register is two's complement in 10 uV steps. */
    assert(opp_ina219_bus_millivolts(0x1f40U) == 4000);
    assert(opp_ina219_bus_millivolts((uint16_t)(1000U << 3U)) == 4000);
    assert(opp_ina219_bus_millivolts((uint16_t)((1000U << 3U) | 0x0003U)) == 4000);
    assert(!opp_ina219_bus_overflow((uint16_t)(1000U << 3U)));
    assert(opp_ina219_bus_overflow(0x0001U));
    assert(opp_ina219_shunt_microvolts(0x0fa0U) == 40000);
    assert(opp_ina219_shunt_microvolts(0xf060U) == -40000);
    /* 40 mV across 0.1 ohm is 400 mA, either way round. */
    assert(near(opp_ina219_current_ma(40000, 100), 400.0f, 0.001f));
    assert(near(opp_ina219_current_ma(-40000, 100), -400.0f, 0.001f));
    assert(near(opp_ina219_current_ma(10, 100), 0.1f, 0.0001f));
    assert(opp_ina219_current_ma(40000, 0) == 0.0f);
}

static void test_battery_voltage_curve(void)
{
    assert(opp_battery_percent_from_millivolts(3000) == 0.0f);
    assert(opp_battery_percent_from_millivolts(3270) == 0.0f);
    assert(opp_battery_percent_from_millivolts(3840) == 50.0f);
    assert(opp_battery_percent_from_millivolts(4200) == 100.0f);
    assert(opp_battery_percent_from_millivolts(4350) == 100.0f);
    /* Halfway between 3.84 V (50 %) and 3.85 V (55 %). */
    assert(near(opp_battery_percent_from_millivolts(3845), 52.5f, 0.001f));

    assert(opp_battery_classify(100.0f) == OPP_BATTERY_CHARGING);
    assert(opp_battery_classify(-100.0f) == OPP_BATTERY_DISCHARGING);
    assert(opp_battery_classify(5.0f) == OPP_BATTERY_IDLE);
    assert(opp_battery_classify(-5.0f) == OPP_BATTERY_IDLE);
}

static void test_battery_gauge(void)
{
    opp_battery_gauge_t gauge;
    opp_battery_gauge_init(&gauge, 5000.0f);

    /* The first sample seeds the count from voltage: 3.84 V is 50 %. */
    opp_battery_gauge_update(&gauge, 3840, -100.0f, 1.0f);
    assert(gauge.seeded && !gauge.calibrated);
    assert(near(opp_battery_gauge_percent(&gauge), 50.0f, 0.001f));

    /* An hour at 100 mA out of the pack removes 100 mAh (2 %). */
    for (int second = 0; second < 3600; ++second) {
        opp_battery_gauge_update(&gauge, 3830, -100.0f, 1.0f);
    }
    assert(near(gauge.charge_mah, 2400.0f, 0.5f));
    assert(near(gauge.average_current_ma, -100.0f, 0.1f));
    float hours;
    assert(opp_battery_gauge_hours_to_empty(&gauge, &hours));
    assert(near(hours, 24.0f, 0.05f));
    assert(!opp_battery_gauge_hours_to_full(&gauge, &hours));

    /* Charging at 1 A: the count rises, and time to full is (5000-charge)/1000. */
    for (int second = 0; second < 1800; ++second) {
        opp_battery_gauge_update(&gauge, 4000, 1000.0f, 1.0f);
    }
    assert(near(gauge.charge_mah, 2900.0f, 1.0f));
    assert(opp_battery_gauge_hours_to_full(&gauge, &hours));
    assert(hours > 1.9f && hours < 2.3f);
    assert(!gauge.calibrated);

    /* A weak charger's whole charge runs below C/20; at 4.16 V that is the
     * constant-voltage top-up, not a full pack (seen on the XIAO's charger). */
    for (int second = 0; second < 600; ++second) {
        opp_battery_gauge_update(&gauge, 4160, 245.0f, 1.0f);
    }
    assert(!gauge.calibrated);

    /* At 4.2 V below C/20, but not yet for a whole minute. */
    for (int second = 0; second < 59; ++second) {
        opp_battery_gauge_update(&gauge, 4200, 200.0f, 1.0f);
    }
    assert(!gauge.calibrated);
    /* A burst above the taper restarts the hold. */
    opp_battery_gauge_update(&gauge, 4200, 400.0f, 1.0f);
    for (int second = 0; second < 30; ++second) {
        opp_battery_gauge_update(&gauge, 4200, 200.0f, 1.0f);
    }
    assert(!gauge.calibrated);

    /* Held for a minute: the pack is full, and the count is pinned to 100 %
     * whatever it had drifted to. */
    for (int second = 0; second < 31; ++second) {
        opp_battery_gauge_update(&gauge, 4200, 200.0f, 1.0f);
    }
    assert(gauge.calibrated);
    assert(opp_battery_gauge_percent(&gauge) == 100.0f);

    /* Never below empty or above full. */
    for (int second = 0; second < 1000; ++second) {
        opp_battery_gauge_update(&gauge, 4200, 5000.0f, 10.0f);
    }
    assert(opp_battery_gauge_percent(&gauge) == 100.0f);
    opp_battery_gauge_update(&gauge, 3300, -100000.0f, 3600.0f);
    assert(opp_battery_gauge_percent(&gauge) == 0.0f);
}

static void test_battery_gauge_follows_unplugging(void)
{
    opp_battery_gauge_t gauge;
    opp_battery_gauge_init(&gauge, 5000.0f);
    opp_battery_gauge_update(&gauge, 4180, 250.0f, 1.0f);
    for (int second = 0; second < 600; ++second) {
        opp_battery_gauge_update(&gauge, 4180, 250.0f, 1.0f);
    }
    /* Unplugged: the next discharging sample must not leave a charging
     * average behind (it once showed six minutes "until full"). */
    opp_battery_gauge_update(&gauge, 4160, -100.0f, 1.0f);
    float hours;
    assert(!opp_battery_gauge_hours_to_full(&gauge, &hours));
    assert(opp_battery_gauge_hours_to_empty(&gauge, &hours));
    assert(hours > 40.0f);

    /* Load pulses within one direction still average normally. */
    opp_battery_gauge_update(&gauge, 4160, -150.0f, 1.0f);
    assert(gauge.average_current_ma < -100.0f && gauge.average_current_ma > -101.0f);
}

static void test_battery_gauge_ignores_interrupted_charge(void)
{
    opp_battery_gauge_t gauge;
    opp_battery_gauge_init(&gauge, 5000.0f);
    opp_battery_gauge_update(&gauge, 3900, 500.0f, 1.0f);
    opp_battery_gauge_update(&gauge, 4000, 500.0f, 60.0f);
    /* Unplugged at 4.0 V: back on battery, then a high voltage spike under no
     * charge must not be taken for a completed charge. */
    opp_battery_gauge_update(&gauge, 3990, -100.0f, 1.0f);
    opp_battery_gauge_update(&gauge, 4160, 0.0f, 1.0f);
    assert(!gauge.calibrated);
}

/* The packets below are protocol/fixtures/bthome-v3.json, which the hub's
 * decoder tests read too, so both ends are held to the same bytes. */
static size_t from_hex(const char *hex, uint8_t *output)
{
    size_t size = 0;
    for (; hex[0] != '\0' && hex[1] != '\0'; hex += 2) {
        unsigned int value;
        assert(sscanf(hex, "%2x", &value) == 1);
        output[size++] = (uint8_t)value;
    }
    return size;
}

static opp_bthome_report_t fixture_report(void)
{
    return (opp_bthome_report_t){
        .report_id = 1234,
        .timestamp_valid = true,
        .timestamp_unix_s = 1790457600U, /* 2026-09-26T21:20:00Z */
        .soil_available = true,
        .soil_temperature_tenths_celsius = 235,
        .soil_moisture_tenths_percent = 1000,
        .conductivity_us_cm = 41,
        .air_available = true,
        .air_temperature_tenths_celsius = 272,
        .air_humidity_hundredths_percent = 4420,
        .battery_available = true,
        .battery_percent = 96,
        .battery_millivolts = 4160,
        .soil_extras_available = true,
        .ph_tenths = 68,
        .nitrogen_mg_kg = 1,
        .phosphorus_mg_kg = 2,
        .potassium_mg_kg = 7,
    };
}

static void assert_encodes(size_t (*encode)(const opp_bthome_report_t *, uint8_t *),
                           const opp_bthome_report_t *report, const char *expected_hex)
{
    uint8_t expected[OPP_BTHOME_SERVICE_DATA_MAX_SIZE];
    uint8_t payload[OPP_BTHOME_SERVICE_DATA_MAX_SIZE];
    const size_t expected_size = from_hex(expected_hex, expected);
    assert(encode(report, payload) == expected_size);
    assert(memcmp(payload, expected, expected_size) == 0);
}

static void test_bthome_v3_fixture(void)
{
    opp_bthome_report_t report = fixture_report();
    assert_encodes(opp_bthome_encode_main, &report,
                   "40022e092e2c2f643ed2040000451001500037b86a562900");
    assert_encodes(opp_bthome_encode_supplementary, &report,
                   "4001600c40103ed204000054080144010002000700");

    report.report_id = 1235;
    report.battery_available = false;
    report.soil_extras_available = false;
    assert_encodes(opp_bthome_encode_supplementary, &report, "403ed3040000");

    report = fixture_report();
    report.report_id = 1235;
    report.timestamp_valid = false;
    report.soil_available = false;
    assert_encodes(opp_bthome_encode_main, &report, "402e2c3ed3040000451001");

    uint8_t payload[OPP_BTHOME_SERVICE_DATA_MAX_SIZE];
    assert(opp_bthome_encode_beacon(payload) == 1 && payload[0] == 0x40);
}

static void test_bthome_v3_rules(void)
{
    uint8_t payload[OPP_BTHOME_SERVICE_DATA_MAX_SIZE];
    opp_bthome_report_t report = fixture_report();

    /* The largest packets still fit a legacy advertisement. */
    assert(opp_bthome_encode_main(&report, payload) == OPP_BTHOME_SERVICE_DATA_MAX_SIZE);
    assert(opp_bthome_encode_supplementary(&report, payload) == 21);

    /* Percentages round to the nearest whole point. */
    report = fixture_report();
    report.air_humidity_hundredths_percent = 4449;
    report.soil_moisture_tenths_percent = 325;
    assert(opp_bthome_encode_main(&report, payload) > 0);
    assert(payload[4] == 0x2E && payload[5] == 44 && payload[6] == 0x2F && payload[7] == 33);

    /* Report ID 0 is never valid. */
    report = fixture_report();
    report.report_id = 0;
    assert(opp_bthome_encode_main(&report, payload) == 0);
    assert(opp_bthome_encode_supplementary(&report, payload) == 0);

    /* No soil and no air: no main packet, only a beacon. */
    report = fixture_report();
    report.soil_available = false;
    report.air_available = false;
    assert(opp_bthome_encode_main(&report, payload) == 0);

    /* Nothing else to carry: the supplementary packet still goes, with the
     * report ID alone, so the hub can tell the report is complete. */
    report = fixture_report();
    report.battery_available = false;
    report.soil_extras_available = false;
    assert(opp_bthome_encode_supplementary(&report, payload) == 6);

    /* Out-of-range values are refused rather than sent. */
    report = fixture_report();
    report.soil_moisture_tenths_percent = 1001;
    assert(opp_bthome_encode_main(&report, payload) == 0);
    report = fixture_report();
    report.ph_tenths = 141;
    assert(opp_bthome_encode_supplementary(&report, payload) == 0);
    report = fixture_report();
    report.battery_percent = 101;
    assert(opp_bthome_encode_supplementary(&report, payload) == 0);

    /* A negative soil temperature is two's complement in hundredths. */
    report = fixture_report();
    report.soil_temperature_tenths_celsius = -52;
    assert(opp_bthome_encode_main(&report, payload) > 0);
    assert(payload[1] == 0x02 && payload[2] == 0xf8 && payload[3] == 0xfd);
}

static void test_report_acknowledgement(void)
{
    /* protocol/fixtures/bthome-v3.json "acknowledgement_v3". */
    const uint8_t expected[] = {0x03, 0xd2, 0x04, 0x00, 0x00};
    uint8_t payload[OPP_REPORT_ACK_PAYLOAD_SIZE];
    uint32_t report_id;

    assert(opp_report_ack_encode(1234, payload) == sizeof(expected));
    assert(memcmp(payload, expected, sizeof(expected)) == 0);
    assert(opp_report_ack_decode(payload, sizeof(payload), &report_id));
    assert(report_id == 1234);

    /* The forced-report tokens of versions 1 and 2 are gone, as is ID 0. */
    const uint8_t version_two[] = {0x02, 0x04, 0x03, 0x02, 0x01, 0xd2, 0x04, 0x00, 0x00};
    assert(!opp_report_ack_decode(version_two, sizeof(version_two), &report_id));
    const uint8_t zero[] = {0x03, 0x00, 0x00, 0x00, 0x00};
    assert(!opp_report_ack_decode(zero, sizeof(zero), &report_id));
    assert(opp_report_ack_encode(0, payload) == 0);
}

/* An in-memory stand-in for flash, with a switch to make writes fail. */
typedef struct {
    bool have_counters;
    uint32_t head;
    uint32_t tail;
    opp_queued_report_t slots[4];
    bool fail_writes;
    bool fail_counter_writes;
} memory_store_t;

static bool memory_load_counters(void *context, uint32_t *head, uint32_t *tail)
{
    memory_store_t *store = context;
    *head = store->head;
    *tail = store->tail;
    return store->have_counters;
}

static bool memory_save_counters(void *context, uint32_t head, uint32_t tail)
{
    memory_store_t *store = context;
    if (store->fail_writes || store->fail_counter_writes) {
        return false;
    }
    store->head = head;
    store->tail = tail;
    store->have_counters = true;
    return true;
}

static bool memory_load_entry(void *context, uint32_t slot, opp_queued_report_t *entry)
{
    memory_store_t *store = context;
    *entry = store->slots[slot];
    return true;
}

static bool memory_save_entry(void *context, uint32_t slot, const opp_queued_report_t *entry)
{
    memory_store_t *store = context;
    if (store->fail_writes) {
        return false;
    }
    store->slots[slot] = *entry;
    return true;
}

static opp_report_queue_storage_t memory_storage(memory_store_t *store)
{
    return (opp_report_queue_storage_t){
        .context = store,
        .load_counters = memory_load_counters,
        .save_counters = memory_save_counters,
        .load_entry = memory_load_entry,
        .save_entry = memory_save_entry,
    };
}

static opp_queued_report_t queued(uint32_t report_id)
{
    opp_queued_report_t entry = {.report_id = report_id, .main_size = 1, .supplementary_size = 1};
    entry.main[0] = (uint8_t)report_id;
    return entry;
}

static bool push(opp_report_queue_t *queue, uint32_t report_id)
{
    const opp_queued_report_t entry = queued(report_id);
    return opp_report_queue_push(queue, &entry);
}

static void test_report_queue(void)
{
    memory_store_t store = {0};
    const opp_report_queue_storage_t storage = memory_storage(&store);
    opp_report_queue_t queue;
    opp_queued_report_t head;

    assert(opp_report_queue_open(&queue, &storage, 4));
    assert(opp_report_queue_depth(&queue) == 0);
    assert(!opp_report_queue_head(&queue, &head));
    /* Nothing queued: any acknowledgement names something delivered before. */
    assert(opp_report_queue_ack(&queue, 7) == OPP_REPORT_ACK_ALREADY);

    for (uint32_t id = 10; id < 14; ++id) {
        assert(push(&queue, id));
    }
    /* Full: refused, never overwritten. */
    assert(opp_report_queue_full(&queue));
    assert(!push(&queue, 14));
    assert(opp_report_queue_head(&queue, &head) && head.report_id == 10);

    /* Only the oldest can be removed. */
    assert(opp_report_queue_ack(&queue, 12) == OPP_REPORT_ACK_REFUSED);
    assert(opp_report_queue_ack(&queue, 99) == OPP_REPORT_ACK_REFUSED);
    assert(opp_report_queue_ack(&queue, 10) == OPP_REPORT_ACK_REMOVED);
    assert(opp_report_queue_head(&queue, &head) && head.report_id == 11);
    /* A repeated acknowledgement is harmless. */
    assert(opp_report_queue_ack(&queue, 10) == OPP_REPORT_ACK_ALREADY);
    assert(opp_report_queue_depth(&queue) == 3);

    /* The ring reuses the freed slot. */
    assert(push(&queue, 14));
    assert(opp_report_queue_full(&queue));

    /* A restart continues exactly where the stored counters say. */
    opp_report_queue_t reopened;
    assert(opp_report_queue_open(&reopened, &storage, 4));
    assert(opp_report_queue_depth(&reopened) == 4);
    for (uint32_t id = 11; id <= 14; ++id) {
        assert(opp_report_queue_head(&reopened, &head) && head.report_id == id);
        assert(head.main[0] == (uint8_t)id);
        assert(opp_report_queue_ack(&reopened, id) == OPP_REPORT_ACK_REMOVED);
    }
    assert(opp_report_queue_depth(&reopened) == 0);
}

static void test_report_queue_failures(void)
{
    memory_store_t store = {0};
    const opp_report_queue_storage_t storage = memory_storage(&store);
    opp_report_queue_t queue;
    assert(opp_report_queue_open(&queue, &storage, 4));
    assert(push(&queue, 1));

    /* The entry saved but not its counter (a reset in between): the report
     * is not part of the queue, before or after a restart. */
    store.fail_counter_writes = true;
    assert(!push(&queue, 2));
    assert(opp_report_queue_depth(&queue) == 1);
    opp_report_queue_t reopened;
    assert(opp_report_queue_open(&reopened, &storage, 4));
    assert(opp_report_queue_depth(&reopened) == 1);

    /* An acknowledgement that cannot be saved removes nothing. */
    assert(opp_report_queue_ack(&queue, 1) == OPP_REPORT_ACK_REFUSED);
    assert(opp_report_queue_depth(&queue) == 1);
    store.fail_counter_writes = false;
    assert(opp_report_queue_ack(&queue, 1) == OPP_REPORT_ACK_REMOVED);

    /* Inconsistent stored counters start an empty queue rather than a wild one. */
    store.head = 9;
    store.tail = 2;
    assert(opp_report_queue_open(&reopened, &storage, 4));
    assert(opp_report_queue_depth(&reopened) == 0);
    store.head = 0;
    store.tail = 50;
    assert(opp_report_queue_open(&reopened, &storage, 4));
    assert(opp_report_queue_depth(&reopened) == 0);

    /* Clearing (a release by the hub) empties it durably. */
    store = (memory_store_t){0};
    assert(opp_report_queue_open(&queue, &storage, 4));
    assert(push(&queue, 5));
    assert(opp_report_queue_clear(&queue));
    assert(opp_report_queue_open(&reopened, &storage, 4));
    assert(opp_report_queue_depth(&reopened) == 0);
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
    test_modbus_generic_read();
    test_soil_probe_request();
    test_ina219_decode();
    test_battery_voltage_curve();
    test_battery_gauge();
    test_battery_gauge_follows_unplugging();
    test_battery_gauge_ignores_interrupted_charge();
    test_soil_probe_response();
    test_soil_probe_negative_temperature();
    test_soil_probe_rejects_out_of_range();
    test_bthome_v3_fixture();
    test_bthome_v3_rules();
    test_bthome_identity();
    test_device_configuration();
    test_report_acknowledgement();
    test_report_queue();
    test_report_queue_failures();
    test_sht45_response();
    test_sht45_humidity_clamping();
    test_clock_policy();
    test_power_source_policy();
    test_wifi_credentials();
    test_firmware_update_command();
    test_firmware_update_url();
    test_firmware_status_suffix();
    return 0;
}