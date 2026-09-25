#include "bthome_payload.h"

#include <stdio.h>

size_t opp_bthome_encode_service_data(const opp_sensor_reading_t *reading,
                                      uint8_t output[OPP_BTHOME_SERVICE_DATA_SIZE])
{
    if (reading == NULL || output == NULL) {
        return 0;
    }

    const int16_t temperature_hundredths =
        (int16_t)(reading->temperature_tenths_celsius * 10);
    const uint16_t moisture_hundredths =
        (uint16_t)(reading->moisture_tenths_percent * 10U);

    output[0] = 0x40;
    output[1] = 0x02;
    output[2] = (uint8_t)temperature_hundredths;
    output[3] = (uint8_t)((uint16_t)temperature_hundredths >> 8U);
    output[4] = 0x14;
    output[5] = (uint8_t)moisture_hundredths;
    output[6] = (uint8_t)(moisture_hundredths >> 8U);
    output[7] = 0x56;
    output[8] = (uint8_t)reading->conductivity_us_cm;
    output[9] = (uint8_t)(reading->conductivity_us_cm >> 8U);
    return OPP_BTHOME_SERVICE_DATA_SIZE;
}

static void append_uint16(uint8_t output[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE],
                          size_t *offset, uint8_t object_id, uint16_t value)
{
    output[(*offset)++] = object_id;
    output[(*offset)++] = (uint8_t)value;
    output[(*offset)++] = (uint8_t)(value >> 8U);
}

size_t opp_bthome_encode_v2_beacon(
    uint8_t packet_id,
    uint8_t output[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE])
{
    if (output == NULL) {
        return 0;
    }
    size_t offset = 0;
    output[offset++] = 0x40;
    output[offset++] = 0x00;
    output[offset++] = packet_id;
    return offset;
}

size_t opp_bthome_encode_v2_service_data(
    const opp_bthome_sample_t *sample,
    uint8_t output[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE])
{
    if (sample == NULL || output == NULL ||
        (!sample->soil_available && !sample->air_available) ||
        (sample->soil_available && sample->soil.moisture_tenths_percent > 1000U) ||
        (sample->air_available && sample->air_humidity_hundredths_percent > 10000U)) {
        return 0;
    }

    size_t offset = 0;
    output[offset++] = 0x40;
    output[offset++] = 0x00;
    output[offset++] = sample->packet_id;
    if (sample->soil_available) {
        append_uint16(output, &offset, 0x02,
                      (uint16_t)(sample->soil.temperature_tenths_celsius * 10));
    }
    if (sample->air_available) {
        append_uint16(output, &offset, 0x03,
                      sample->air_humidity_hundredths_percent);
    }
    if (sample->soil_available) {
        append_uint16(output, &offset, 0x14,
                      (uint16_t)(sample->soil.moisture_tenths_percent * 10U));
    }
    if (sample->button_event) {
        output[offset++] = 0x3a;
        output[offset++] = 0x01;
    }
    if (sample->air_available) {
        append_uint16(output, &offset, 0x45,
                      (uint16_t)sample->air_temperature_tenths_celsius);
    }
    if (sample->soil_available) {
        append_uint16(output, &offset, 0x56, sample->soil.conductivity_us_cm);
    }
    return offset;
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