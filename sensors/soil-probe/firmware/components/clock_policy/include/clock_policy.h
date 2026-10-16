#pragma once

#include <stdbool.h>
#include <stdint.h>

#define OPP_CLOCK_EARLIEST_UNIX_SECONDS INT64_C(1704067200)
#define OPP_CLOCK_LATEST_UNIX_SECONDS INT64_C(4102444800)

bool opp_clock_epoch_is_plausible(int64_t unix_seconds);
bool opp_clock_resync_is_due(int64_t now_unix_seconds,
                             int64_t last_sync_unix_seconds,
                             int64_t interval_seconds);
int64_t opp_clock_adjustment_ms(int64_t before_unix_ms,
                                int64_t elapsed_monotonic_us,
                                int64_t after_unix_ms);