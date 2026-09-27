#include "bthome_payload.h"

#include <stdio.h>

#define BTHOME_DEVICE_INFO 0x40U

static void append_u8(uint8_t *output, size_t *offset, uint8_t object_id, uint8_t value)
{
    output[(*offset)++] = object_id;
    output[(*offset)++] = value;
}

static void append_u16(uint8_t *output, size_t *offset, uint8_t object_id, uint16_t value)
{
    output[(*offset)++] = object_id;
    output[(*offset)++] = (uint8_t)value;
    output[(*offset)++] = (uint8_t)(value >> 8U);
}

static void append_u32(uint8_t *output, size_t *offset, uint8_t object_id, uint32_t value)
{
    output[(*offset)++] = object_id;
    output[(*offset)++] = (uint8_t)value;
    output[(*offset)++] = (uint8_t)(value >> 8U);
    output[(*offset)++] = (uint8_t)(value >> 16U);
    output[(*offset)++] = (uint8_t)(value >> 24U);
}

static void put_u16(uint8_t *output, size_t *offset, uint16_t value)
{
    output[(*offset)++] = (uint8_t)value;
    output[(*offset)++] = (uint8_t)(value >> 8U);
}

/* Whole percent, rounded to nearest, from tenths or hundredths. */
static uint8_t whole_percent(uint32_t value, uint32_t per_percent)
{
    return (uint8_t)((value + per_percent / 2U) / per_percent);
}

size_t opp_bthome_encode_packet1(const opp_bthome_report_t *report,
                                 uint8_t output[OPP_BTHOME_SERVICE_DATA_MAX_SIZE])
{
    if (report == NULL || output == NULL || report->report_id == 0 ||
        (!report->soil_available && !report->air_available) ||
        (report->soil_available && report->soil_moisture_tenths_percent > 1000U) ||
        (report->air_available && report->air_humidity_hundredths_percent > 10000U)) {
        return 0;
    }

    size_t offset = 0;
    output[offset++] = BTHOME_DEVICE_INFO;
    if (report->soil_available) {
        append_u16(output, &offset, 0x02,
                   (uint16_t)(int16_t)(report->soil_temperature_tenths_celsius * 10));
    }
    if (report->air_available) {
        append_u8(output, &offset, 0x2E,
                  whole_percent(report->air_humidity_hundredths_percent, 100U));
    }
    if (report->soil_available) {
        append_u8(output, &offset, 0x2F,
                  whole_percent(report->soil_moisture_tenths_percent, 10U));
    }
    append_u32(output, &offset, 0x3E, report->report_id);
    if (report->air_available) {
        append_u16(output, &offset, 0x45, (uint16_t)report->air_temperature_tenths_celsius);
    }
    if (report->timestamp_valid) {
        append_u32(output, &offset, 0x50, report->timestamp_unix_s);
    }
    if (report->soil_available) {
        append_u16(output, &offset, 0x56, report->conductivity_us_cm);
    }
    return offset;
}

size_t opp_bthome_encode_packet2(const opp_bthome_report_t *report,
                                 uint8_t output[OPP_BTHOME_SERVICE_DATA_MAX_SIZE])
{
    if (report == NULL || output == NULL || report->report_id == 0 ||
        (report->battery_available && report->battery_percent > 100U) ||
        (report->soil_extras_available && report->ph_tenths > 140U)) {
        return 0;
    }

    size_t offset = 0;
    output[offset++] = BTHOME_DEVICE_INFO;
    if (report->battery_available) {
        append_u8(output, &offset, 0x01, report->battery_percent);
        append_u16(output, &offset, 0x0C, report->battery_millivolts);
        append_u8(output, &offset, 0x16, report->battery_charging ? 1U : 0U);
    }
    append_u32(output, &offset, 0x3E, report->report_id);
    if (report->soil_extras_available) {
        /* BTHome has no pH or nutrient objects, so they travel as one raw
         * object in a layout only the hub decodes. */
        output[offset++] = 0x54;
        output[offset++] = 8;
        output[offset++] = OPP_BTHOME_SOIL_EXTRAS_LAYOUT_VERSION;
        output[offset++] = (uint8_t)report->ph_tenths;
        put_u16(output, &offset, report->nitrogen_mg_kg);
        put_u16(output, &offset, report->phosphorus_mg_kg);
        put_u16(output, &offset, report->potassium_mg_kg);
    }
    return offset;
}

size_t opp_bthome_encode_beacon(uint8_t output[OPP_BTHOME_SERVICE_DATA_MAX_SIZE])
{
    if (output == NULL) {
        return 0;
    }
    output[0] = BTHOME_DEVICE_INFO;
    return 1;
}

bool opp_bthome_format_local_name(uint64_t device_id,
                                  char output[OPP_BTHOME_LOCAL_NAME_SIZE])
{
    if (output == NULL || device_id > UINT64_C(0xffffffffffff)) {
        return false;
    }
    return snprintf(output, OPP_BTHOME_LOCAL_NAME_SIZE, "sensor-%012llx",
                    (unsigned long long)device_id) == OPP_BTHOME_LOCAL_NAME_SIZE - 1;
}
