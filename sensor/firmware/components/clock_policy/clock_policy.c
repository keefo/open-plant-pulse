#include "clock_policy.h"

bool opp_clock_epoch_is_plausible(int64_t unix_seconds)
{
    return unix_seconds >= OPP_CLOCK_EARLIEST_UNIX_SECONDS &&
           unix_seconds < OPP_CLOCK_LATEST_UNIX_SECONDS;
}

bool opp_clock_resync_is_due(int64_t now_unix_seconds,
                             int64_t last_sync_unix_seconds,
                             int64_t interval_seconds)
{
    if (!opp_clock_epoch_is_plausible(now_unix_seconds) ||
        !opp_clock_epoch_is_plausible(last_sync_unix_seconds) ||
        interval_seconds <= 0 || now_unix_seconds < last_sync_unix_seconds) {
        return true;
    }
    return now_unix_seconds - last_sync_unix_seconds >= interval_seconds;
}

int64_t opp_clock_adjustment_ms(int64_t before_unix_ms,
                                int64_t elapsed_monotonic_us,
                                int64_t after_unix_ms)
{
    return after_unix_ms - before_unix_ms - elapsed_monotonic_us / 1000;
}