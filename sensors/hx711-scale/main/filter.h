#pragma once
// Smoothing for raw HX711 samples (10 per second), in counts:
//   1. median of the last 5 samples, which drops one-off spikes
//   2. exponential moving average that snaps to the new value when the weight clearly changes
//   3. stability: the last second of filtered values stays within one display step
#include <stdbool.h>
#include <stdint.h>

#define FILTER_MEDIAN_N  5
#define FILTER_STABLE_N  10    // 1 s at 10 samples/s
#define FILTER_DRIFT_N   30    // 30 s, sampled once a second

typedef struct {
    int32_t window[FILTER_MEDIAN_N];
    int n, next;
    float value;                  // filtered counts
    bool primed;
    float recent[FILTER_STABLE_N];
    int rn, rnext;
    bool stable;
    float spread;                 // counts between the highest and lowest of the last second
    // How far the smoothed value wanders over half a minute. This, not the
    // spread over one second, is what makes a steady scale flicker between
    // two displayed values: the reading drifts across a rounding boundary
    // long after each individual second looks perfectly quiet.
    float drift[FILTER_DRIFT_N];
    int dn, dnext, dcount;
} filter_t;

void filter_reset(filter_t *f);
// How far the reading wanders over the last half minute, in counts. Measured
// rather than assumed, because the constant that was assumed turned out to
// be smaller than this cell's actual wander.
float filter_drift(const filter_t *f);
// Feeds one raw sample and returns the filtered value. step_counts is one display step in counts.
float filter_add(filter_t *f, int32_t raw, float step_counts);
