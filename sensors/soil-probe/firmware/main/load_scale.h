#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/* An HX711 reading a bar load cell, or several wired in parallel, under a
 * plant's pot. Weight is what the pot holds; a dry-down shows up as grams lost
 * since the last watering, with none of the substrate calibration the soil
 * probe needs.
 *
 * Strain gauges measure absolute deflection, so the cell keeps no state across
 * a power cut: the HX711 can stay powered down between wake cycles and still
 * report the standing load on the next read. Only the tare offset and the
 * scale factor are stored, and both survive in NVS. */

typedef struct {
    int32_t raw;        /* Averaged 24-bit conversion, sign-extended. */
    int32_t grams;      /* Zero when the scale has not been calibrated. */
    bool calibrated;
} opp_load_scale_reading_t;

/* Configures the GPIOs and leaves the HX711 powered down. */
esp_err_t opp_load_scale_init(void);

/* Powers the HX711 up, discards the settling conversions, averages
 * CONFIG_OPP_LOAD_SCALE_SAMPLES of them, and powers it back down. */
esp_err_t opp_load_scale_read(opp_load_scale_reading_t *reading);

/* Drives PD_SCK high so the HX711 and the bridge excitation both stop. Safe to
 * call when uninitialised, so deep sleep can call it unconditionally. */
void opp_load_scale_power_down(void);

/* Records the current raw reading as the empty-platform zero. Run with the
 * platform built but the pot not yet on it. */
esp_err_t opp_load_scale_tare(void);

/* Records counts-per-gram from a known mass resting on the platform, on top of
 * whatever the tare captured. Rejects a known mass of zero. */
esp_err_t opp_load_scale_calibrate(int32_t known_grams);

bool opp_load_scale_is_calibrated(void);
