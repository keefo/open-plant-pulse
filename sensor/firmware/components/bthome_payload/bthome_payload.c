#include "bthome_payload.h"

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