#pragma once
// Smoothing for raw HX711 samples (10 per second), in counts:
//   1. median of the last 5 samples, which drops one-off spikes
//   2. exponential moving average that snaps to the new value when the weight clearly changes
//   3. stability: the last second of filtered values stays within one display step
#include <stdbool.h>
#include <stdint.h>

#define FILTER_MEDIAN_N  5
#define FILTER_STABLE_N  10    // 1 s at 10 samples/s

typedef struct {
    int32_t window[FILTER_MEDIAN_N];
    int n, next;
    float value;                  // filtered counts
    bool primed;
    float recent[FILTER_STABLE_N];
    int rn, rnext;
    bool stable;
} filter_t;

void filter_reset(filter_t *f);
// Feeds one raw sample and returns the filtered value. step_counts is one display step in counts.
float filter_add(filter_t *f, int32_t raw, float step_counts);
