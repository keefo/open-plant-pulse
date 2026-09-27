#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Battery current is signed from the pack's point of view: positive flows into
 * it (charging), negative out of it (the node running on battery). */
typedef enum {
    OPP_BATTERY_DISCHARGING,
    OPP_BATTERY_IDLE,
    OPP_BATTERY_CHARGING,
} opp_battery_flow_t;

/* Below this magnitude the pack is neither charging nor discharging. */
#define OPP_BATTERY_IDLE_THRESHOLD_MA 15.0f
/* Charging is complete once the pack has held at least this voltage with the
 * charge current tapered below capacity / OPP_BATTERY_TAPER_DIVISOR (C/20) for
 * OPP_BATTERY_FULL_HOLD_SECONDS. The hold matters: a weak charger (the XIAO's
 * own, about 250 mA) runs below C/20 for its whole charge, and at 4.16 V a
 * pack is still in its constant-voltage top-up, not full. */
#define OPP_BATTERY_FULL_MILLIVOLTS 4180
#define OPP_BATTERY_TAPER_DIVISOR 20.0f
#define OPP_BATTERY_FULL_HOLD_SECONDS 60.0f
/* The average used for runtime estimates follows about five minutes. */
#define OPP_BATTERY_AVERAGE_SECONDS 300.0f

typedef struct {
    float capacity_mah;
    float charge_mah;
    float average_current_ma;
    bool seeded;
    /* Set once a completed charge has pinned the count to 100 %. Until then
     * the level started from a voltage guess and is only an estimate. */
    bool calibrated;
    bool charging_seen;
    float full_seconds;
} opp_battery_gauge_t;

/* Open-circuit voltage to state of charge for a Li-ion (NMC) cell, 0-100. It is
 * only accurate at rest: load pulls the voltage down and charging pushes it
 * up. */
float opp_battery_percent_from_millivolts(int32_t millivolts);

opp_battery_flow_t opp_battery_classify(float current_ma);
const char *opp_battery_flow_name(opp_battery_flow_t flow);

void opp_battery_gauge_init(opp_battery_gauge_t *gauge, float capacity_mah);

/* Account for `elapsed_seconds` at the given pack voltage and current. */
void opp_battery_gauge_update(opp_battery_gauge_t *gauge, int32_t millivolts,
                              float current_ma, float elapsed_seconds);

float opp_battery_gauge_percent(const opp_battery_gauge_t *gauge);

/* Hours until empty at the recent average draw; false unless discharging. */
bool opp_battery_gauge_hours_to_empty(const opp_battery_gauge_t *gauge, float *hours);
/* Hours until full at the recent average charge current; false unless
 * charging. Optimistic, since the current tapers near the end. */
bool opp_battery_gauge_hours_to_full(const opp_battery_gauge_t *gauge, float *hours);
