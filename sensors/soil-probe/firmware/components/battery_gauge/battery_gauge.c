#include "battery_gauge.h"

#include <stddef.h>

/* A widely used resting-voltage curve for 18650 NMC cells. */
static const struct {
    int32_t millivolts;
    float percent;
} voltage_curve[] = {
    {3270, 0.0f},  {3610, 5.0f},  {3690, 10.0f}, {3710, 15.0f}, {3730, 20.0f},
    {3750, 25.0f}, {3770, 30.0f}, {3790, 35.0f}, {3800, 40.0f}, {3820, 45.0f},
    {3840, 50.0f}, {3850, 55.0f}, {3870, 60.0f}, {3910, 65.0f}, {3950, 70.0f},
    {3980, 75.0f}, {4020, 80.0f}, {4080, 85.0f}, {4110, 90.0f}, {4150, 95.0f},
    {4200, 100.0f},
};

float opp_battery_percent_from_millivolts(int32_t millivolts)
{
    const size_t count = sizeof(voltage_curve) / sizeof(voltage_curve[0]);
    if (millivolts <= voltage_curve[0].millivolts) {
        return 0.0f;
    }
    if (millivolts >= voltage_curve[count - 1].millivolts) {
        return 100.0f;
    }
    for (size_t index = 1; index < count; ++index) {
        if (millivolts <= voltage_curve[index].millivolts) {
            const int32_t low_mv = voltage_curve[index - 1].millivolts;
            const int32_t high_mv = voltage_curve[index].millivolts;
            const float low = voltage_curve[index - 1].percent;
            const float high = voltage_curve[index].percent;
            return low + (high - low) * (float)(millivolts - low_mv) /
                             (float)(high_mv - low_mv);
        }
    }
    return 100.0f;
}

opp_battery_flow_t opp_battery_classify(float current_ma)
{
    if (current_ma > OPP_BATTERY_IDLE_THRESHOLD_MA) {
        return OPP_BATTERY_CHARGING;
    }
    if (current_ma < -OPP_BATTERY_IDLE_THRESHOLD_MA) {
        return OPP_BATTERY_DISCHARGING;
    }
    return OPP_BATTERY_IDLE;
}

const char *opp_battery_flow_name(opp_battery_flow_t flow)
{
    switch (flow) {
    case OPP_BATTERY_CHARGING:
        return "charging";
    case OPP_BATTERY_DISCHARGING:
        return "discharging";
    default:
        return "idle";
    }
}

void opp_battery_gauge_init(opp_battery_gauge_t *gauge, float capacity_mah)
{
    if (gauge == NULL) {
        return;
    }
    *gauge = (opp_battery_gauge_t){.capacity_mah = capacity_mah};
}

void opp_battery_gauge_update(opp_battery_gauge_t *gauge, int32_t millivolts,
                              float current_ma, float elapsed_seconds)
{
    if (gauge == NULL || gauge->capacity_mah <= 0.0f || elapsed_seconds < 0.0f) {
        return;
    }
    if (!gauge->seeded) {
        gauge->charge_mah =
            gauge->capacity_mah * opp_battery_percent_from_millivolts(millivolts) / 100.0f;
        gauge->average_current_ma = current_ma;
        gauge->seeded = true;
        return;
    }

    gauge->charge_mah += current_ma * elapsed_seconds / 3600.0f;
    if (gauge->charge_mah < 0.0f) {
        gauge->charge_mah = 0.0f;
    } else if (gauge->charge_mah > gauge->capacity_mah) {
        gauge->charge_mah = gauge->capacity_mah;
    }

    /* A reversal (charger plugged in or pulled) starts the average afresh.
     * Letting it drift across over five minutes would report the old
     * direction: minutes "until full" on a pack that is now discharging. */
    const opp_battery_flow_t current_flow = opp_battery_classify(current_ma);
    const opp_battery_flow_t average_flow = opp_battery_classify(gauge->average_current_ma);
    if ((current_flow == OPP_BATTERY_CHARGING && average_flow == OPP_BATTERY_DISCHARGING) ||
        (current_flow == OPP_BATTERY_DISCHARGING && average_flow == OPP_BATTERY_CHARGING)) {
        gauge->average_current_ma = current_ma;
    } else {
        const float weight = elapsed_seconds / (OPP_BATTERY_AVERAGE_SECONDS + elapsed_seconds);
        gauge->average_current_ma += (current_ma - gauge->average_current_ma) * weight;
    }

    /* The one moment the true level is known: a charge that has run to its end.
     * Anchoring there removes the error the starting guess and the integration
     * have accumulated. */
    const opp_battery_flow_t flow = opp_battery_classify(current_ma);
    if (flow == OPP_BATTERY_CHARGING) {
        gauge->charging_seen = true;
    }
    const float taper_ma = gauge->capacity_mah / OPP_BATTERY_TAPER_DIVISOR;
    if (gauge->charging_seen && millivolts >= OPP_BATTERY_FULL_MILLIVOLTS &&
        current_ma < taper_ma) {
        gauge->full_seconds += elapsed_seconds;
    } else {
        gauge->full_seconds = 0.0f;
    }
    if (gauge->full_seconds >= OPP_BATTERY_FULL_HOLD_SECONDS) {
        gauge->charge_mah = gauge->capacity_mah;
        gauge->calibrated = true;
        gauge->charging_seen = false;
        gauge->full_seconds = 0.0f;
    } else if (flow == OPP_BATTERY_DISCHARGING &&
               millivolts < OPP_BATTERY_FULL_MILLIVOLTS) {
        /* Unplugged before the end: that charge proves nothing. */
        gauge->charging_seen = false;
    }
}

float opp_battery_gauge_percent(const opp_battery_gauge_t *gauge)
{
    if (gauge == NULL || gauge->capacity_mah <= 0.0f) {
        return 0.0f;
    }
    return 100.0f * gauge->charge_mah / gauge->capacity_mah;
}

bool opp_battery_gauge_hours_to_empty(const opp_battery_gauge_t *gauge, float *hours)
{
    if (gauge == NULL || hours == NULL || !gauge->seeded ||
        opp_battery_classify(gauge->average_current_ma) != OPP_BATTERY_DISCHARGING) {
        return false;
    }
    *hours = gauge->charge_mah / -gauge->average_current_ma;
    return true;
}

bool opp_battery_gauge_hours_to_full(const opp_battery_gauge_t *gauge, float *hours)
{
    if (gauge == NULL || hours == NULL || !gauge->seeded ||
        opp_battery_classify(gauge->average_current_ma) != OPP_BATTERY_CHARGING) {
        return false;
    }
    *hours = (gauge->capacity_mah - gauge->charge_mah) / gauge->average_current_ma;
    return true;
}
