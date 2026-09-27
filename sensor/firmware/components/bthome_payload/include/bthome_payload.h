#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Contract v3 (protocol/README.md). The largest packet, the main one with
 * every object, is 24 bytes, which fills a 31-byte legacy advertisement once
 * the Flags element and service-data header are added. */
#define OPP_BTHOME_SERVICE_DATA_MAX_SIZE 24
#define OPP_BTHOME_LOCAL_NAME_SIZE 20
#define OPP_BTHOME_SOIL_EXTRAS_LAYOUT_VERSION 1

/* One report: everything measured together, under one report ID. */
typedef struct {
    uint32_t report_id;
    bool timestamp_valid;
    uint32_t timestamp_unix_s;

    bool soil_available;
    int16_t soil_temperature_tenths_celsius;
    uint16_t soil_moisture_tenths_percent;
    uint16_t conductivity_us_cm;

    bool air_available;
    int16_t air_temperature_tenths_celsius;
    uint16_t air_humidity_hundredths_percent;

    bool battery_available;
    uint8_t battery_percent;
    uint16_t battery_millivolts;

    bool soil_extras_available;
    uint16_t ph_tenths;
    uint16_t nitrogen_mg_kg;
    uint16_t phosphorus_mg_kg;
    uint16_t potassium_mg_kg;
} opp_bthome_report_t;

/* The core measurements. Returns 0 when the report has neither soil nor air,
 * or a value out of range: such a report cannot be sent, only a beacon. */
size_t opp_bthome_encode_main(const opp_bthome_report_t *report,
                              uint8_t output[OPP_BTHOME_SERVICE_DATA_MAX_SIZE]);

/* Battery, pH and N/P/K. Every report has one, even if it carries only the
 * report ID: that is how the hub knows the report is complete. Returns 0 only
 * for an invalid report. */
size_t opp_bthome_encode_supplementary(const opp_bthome_report_t *report,
                                       uint8_t output[OPP_BTHOME_SERVICE_DATA_MAX_SIZE]);

/* Presence without a report: device info and nothing else. A sensor with
 * nothing to measure still has to be findable and adoptable. */
size_t opp_bthome_encode_beacon(uint8_t output[OPP_BTHOME_SERVICE_DATA_MAX_SIZE]);

bool opp_bthome_format_local_name(uint64_t device_id,
                                  char output[OPP_BTHOME_LOCAL_NAME_SIZE]);
