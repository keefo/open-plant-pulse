#include <math.h>
#include <string.h>
#include "filter.h"

#define ALPHA_SETTLING  0.25f  // follows changes quickly while the reading moves
#define ALPHA_STABLE    0.08f  // heavier smoothing once it has settled
#define JUMP_STEPS      5      // a change this many display steps away is a new weight

void filter_reset(filter_t *f)
{
    memset(f, 0, sizeof *f);
}

static float median(const filter_t *f)
{
    int32_t s[FILTER_MEDIAN_N];
    memcpy(s, f->window, f->n * sizeof s[0]);
    for (int i = 1; i < f->n; i++)  // insertion sort, at most 5 items
        for (int j = i; j > 0 && s[j - 1] > s[j]; j--) {
            int32_t t = s[j]; s[j] = s[j - 1]; s[j - 1] = t;
        }
    return s[f->n / 2];
}

float filter_add(filter_t *f, int32_t raw, float step_counts)
{
    // 1. median
    f->window[f->next] = raw;
    f->next = (f->next + 1) % FILTER_MEDIAN_N;
    if (f->n < FILTER_MEDIAN_N) f->n++;
    float m = median(f);

    // 2. adaptive moving average
    if (!f->primed || fabsf(m - f->value) > JUMP_STEPS * step_counts) {
        f->value = m;  // weight put on or taken off: follow at once, don't crawl there
        f->primed = true;
        f->rn = 0;
    } else {
        f->value += (f->stable ? ALPHA_STABLE : ALPHA_SETTLING) * (m - f->value);
    }

    // 3. stability over the last second
    f->recent[f->rnext] = f->value;
    f->rnext = (f->rnext + 1) % FILTER_STABLE_N;
    if (f->rn < FILTER_STABLE_N) f->rn++;
    if (f->rn < FILTER_STABLE_N) {
        f->stable = false;
    } else {
        float lo = f->recent[0], hi = f->recent[0];
        for (int i = 1; i < FILTER_STABLE_N; i++) {
            lo = fminf(lo, f->recent[i]);
            hi = fmaxf(hi, f->recent[i]);
        }
        f->spread = hi - lo;
        f->stable = f->spread < step_counts;
    }

    // One sample a second into the drift window, which therefore covers the
    // last half minute.
    if (++f->dn >= FILTER_STABLE_N) {
        f->dn = 0;
        f->drift[f->dnext] = f->value;
        f->dnext = (f->dnext + 1) % FILTER_DRIFT_N;
        if (f->dcount < FILTER_DRIFT_N) f->dcount++;
    }
    return f->value;
}

float filter_drift(const filter_t *f)
{
    if (f->dcount < 3) return 0;      // not enough yet to say
    float lo = f->drift[0], hi = f->drift[0];
    for (int i = 1; i < f->dcount; i++) {
        lo = fminf(lo, f->drift[i]);
        hi = fmaxf(hi, f->drift[i]);
    }
    return hi - lo;
}
