#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "sensor_protocol.h"

typedef struct {
    opp_soil_probe_reading_t values;
    uint64_t sequence;
} opp_soil_probe_sample_record_t;

/* Switch the probe's supply (no-op without a power GPIO). Switching on waits
 * the configured warm-up before returning. */
esp_err_t opp_soil_probe_set_power(bool on);
bool opp_soil_probe_is_powered(void);

/* One-shot access, for a wake cycle that reads once and sleeps. */
esp_err_t opp_soil_probe_init(void);
esp_err_t opp_soil_probe_read(opp_soil_probe_reading_t *reading);
esp_err_t opp_soil_probe_deinit(void);

/* Debug: read `count` raw registers (at most OPP_MODBUS_MAX_READ_REGISTERS)
 * with function 0x03 or 0x04, for finding where a probe keeps its values.
 * ESP_ERR_NOT_SUPPORTED means the probe answered with a Modbus exception.
 *
 * `warmup_ms` holds the probe powered at least that long before reading,
 * for measuring how long a channel needs to settle. The probe's pH is the
 * reason it exists: it reads near the floor of its range two seconds after
 * power-on and climbs for some time after that, and the only way to find
 * out how long is to ask it at a succession of warm-ups. Zero means the
 * built-in warm-up, which is what everything other than that hunt wants. */
#define OPP_SOIL_PROBE_MAX_WARMUP_MS 300000U
esp_err_t opp_soil_probe_read_registers(uint8_t function, uint16_t first_register,
                                        uint16_t count, uint16_t *values,
                                        uint32_t warmup_ms);

/* The last deep reading that nothing has reported yet.
 *
 * pH and the nutrients come from a reading taken a few times a day with a
 * long warm-up, not from every sample, so they are handed over once and
 * then cleared. A report that does not get one says it has no pH rather
 * than repeating the last: a value resent every half hour reads as a
 * measurement every half hour, and 47 of those 48 never happened. */
bool opp_soil_probe_take_deep_reading(opp_soil_probe_reading_t *reading);
/* When the next deep reading is due, for the console. */
int64_t opp_soil_probe_seconds_until_deep(void);

/* Continuous sampling while the development firmware stays awake. */
esp_err_t opp_soil_probe_monitor_start(bool enabled);
bool opp_soil_probe_monitor_is_running(void);
/* The console's Enabled switch: off stops polling and clears the reading. */
void opp_soil_probe_monitor_set_enabled(bool enabled);
bool opp_soil_probe_monitor_is_enabled(void);
bool opp_soil_probe_monitor_get_latest(opp_soil_probe_sample_record_t *sample,
                                       int64_t *sample_age_ms);
/* Why the most recent read failed, or ESP_OK after a success. */
esp_err_t opp_soil_probe_monitor_last_error(void);
