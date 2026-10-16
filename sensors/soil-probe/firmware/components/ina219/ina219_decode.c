#include "ina219_decode.h"

int32_t opp_ina219_bus_millivolts(uint16_t reg)
{
    return (int32_t)(reg >> 3U) * 4;
}

bool opp_ina219_bus_overflow(uint16_t reg)
{
    return (reg & 0x0001U) != 0;
}

int32_t opp_ina219_shunt_microvolts(uint16_t reg)
{
    return (int32_t)(int16_t)reg * 10;
}

float opp_ina219_current_ma(int32_t shunt_microvolts, uint32_t shunt_milliohms)
{
    if (shunt_milliohms == 0) {
        return 0.0f;
    }
    return (float)shunt_microvolts / (float)shunt_milliohms;
}
