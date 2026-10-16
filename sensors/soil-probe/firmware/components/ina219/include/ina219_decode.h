#pragma once

#include <stdbool.h>
#include <stdint.h>

#define OPP_INA219_REG_CONFIG 0x00
#define OPP_INA219_REG_SHUNT_VOLTAGE 0x01
#define OPP_INA219_REG_BUS_VOLTAGE 0x02

/* 16 V bus range, +/-320 mV shunt range, and 128-sample averaging (68 ms) on
 * both channels, converting continuously. The averaging smooths the radio's
 * short current bursts into something one read a second can follow. */
#define OPP_INA219_CONFIG_CONTINUOUS 0x1fffU
/* The same settings with the converter off: about 6 uA instead of 1 mA. */
#define OPP_INA219_CONFIG_POWER_DOWN 0x1ff8U

/* Bus voltage register: bits 15-3 in 4 mV steps, bit 0 set on math overflow. */
int32_t opp_ina219_bus_millivolts(uint16_t reg);
bool opp_ina219_bus_overflow(uint16_t reg);

/* Shunt voltage register: signed, 10 uV per step. */
int32_t opp_ina219_shunt_microvolts(uint16_t reg);

/* Current through the shunt; uV divided by milliohms is mA. */
float opp_ina219_current_ma(int32_t shunt_microvolts, uint32_t shunt_milliohms);
