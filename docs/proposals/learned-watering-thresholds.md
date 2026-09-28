# Learned watering thresholds

## Status

Proposed 2026-09-28. Not implemented. The plant profiles' generic moisture
targets were lowered for the bird of paradise the same day as a stopgap; this
proposal replaces generic targets with ones learned from each sensor's own pot.

## Goal

Tell the user when to water from how *their* pot behaves, not from generic
numbers. After one full watering cycle, each sensor's watering thresholds come
from its own pot, soil and probe depth. The plant profile keeps saying what kind
of plant it is; the pot supplies the numbers.

## Why generic targets fail

The soil probe reports volumetric water content calibrated for mineral soil.
The same plant reads very differently depending on things the profile cannot
know:

- **Soil.** An airy bark and perlite mix holds less water by volume than garden
  soil, and its air gaps lower the reading further. "35 %" in one pot and "35 %"
  in another are not the same condition.
- **Depth and placement.** Near the surface the soil dries first; near the
  bottom water collects. The probe reads the zone around its needles.
- **Pot.** Size, material and drainage change how fast the whole cycle runs.

Measured on the first sensor, a bird of paradise in an airy mix with the probe
10 cm deep: about 17 % six days after watering, in a watering rhythm of about 23
days that suits the plant, and about 53 % two minutes after a watering, still
draining. The strelitzia profile then said refill below 35 % and concern below
20 %, so the hub would have asked for water every three to five days. For a plant
whose main risk is root rot from overwatering, that is the dangerous direction.

## What is learned

Per sensor, from its own readings:

| Value | Meaning | How it is measured |
| --- | --- | --- |
| Full level | Moisture once a thorough watering has drained | Median moisture from 12 to 36 hours after a full watering |
| Refill point | The level at which the user actually waters | Moisture just before the next full watering |
| Dry-down rate | How fast the pot dries | Percentage points per day between the two |

The dry-down rate is not used for thresholds; it is kept for a later "water in
about N days" forecast.

## Recognising a full watering

The hub already records a watering as a care event when moisture rises by at
least 15 points (`WATERING_RISE_PERCENT`), and watches drainage settle
(moisture within 1.5 points for 20 minutes, `DRAINAGE_SETTLE_WINDOW`). A watering counts for learning
only if it is **full**: moisture reaches near saturation and then settles.

A small top-up, like the half cup poured at the probe on 2026-09-27 to test its
contact, rises steeply at the probe but does not wet the pot through. Those are
marked partial and ignored, so they cannot pull the full level or the refill
point. What "near saturation" means for a given pot is itself learned: the first
cycles use a generous rule (a rise of at least 25 points, peaking above 60 %),
and later ones compare against the pot's own previous peaks.

## Deriving thresholds

| Threshold | Derived from |
| --- | --- |
| Refill below | The learned refill point |
| Comfortable cycle | Refill point up to full level |
| Target after watering | Full level, plus or minus a few points |
| Concern below | About 60 % of the refill point: overdue |
| Concern above | Full level plus a margin, **sustained beyond the settle time**: not draining |

"Concern above" is the warning that matters most for a plant prone to rot. It
fires only when the pot stays wetter than its own full level after drainage
should have finished, never during the minutes of soaking right after a
watering.

The profile's defaults act as a fence, not the source. A learned value is
adopted only if it is plausible: the refill point between concern-low and the
full level, and the full level above the refill point by a clear margin. If it
falls far outside the plant's plausible band, the hub asks the user rather than
adopting it silently.

## Adapting over time

The values are a rolling average over the last three complete cycles. Summer
drying faster, winter slower, and a growing plant all move the numbers smoothly;
one odd cycle cannot swing them. A cycle is complete when a full watering is
followed by another full watering.

## States

On the sensor's page:

- **Calibrating: first cycle.** The profile's defaults are in use until one
  complete cycle has been observed. The page says so and what it is waiting for
  ("next full watering").
- **Learned from N cycles.** The learned values, with the dates of the cycles
  they came from.
- **Manual.** The user sets the numbers; learning continues in the background
  but does not replace them. This extends the existing per-sensor
  `moisture_low_percent` override to the whole set.
- **Recalibrate.** For after moving the probe or repotting: learning starts over,
  because the old numbers describe a different placement.

## Storage

A small per-sensor calibration table: full level, refill point, dry-down rate,
the number of cycles and the report IDs that bound each, last updated, and mode
(calibrating, learned, manual). One forward migration, rehearsed on a copy of the
live database and backed up before it runs, as every migration so far.

The care-event detector (`domain/care_events.py`) keeps using a single
`refill_below` per sensor; it reads it from the calibration when one exists,
from the manual value in manual mode, and from the profile otherwise.

## Plan

1. Classify waterings as full or partial, from the existing watering and
   drainage observations, and record the classification.
2. Compute full level, refill point and dry-down rate per complete cycle, and the
   rolling values.
3. Derive thresholds with the plausibility fence; store them with their mode.
4. Feed the care-event detector and the page from the stored thresholds.
5. Sensor page: the state, the learned values and their cycles, Manual and
   Recalibrate.

## Validation

- Unit tests on synthetic cycles: full and partial waterings, a noisy cycle, a
  seasonal drift, a recalibration, a value outside the plausible band.
- Replaying the first sensor's real history once it has a couple of complete
  cycles. With a rhythm of about 23 days, the first learned values arrive after
  the next full watering and the one after it.
- On the page: every state rendered, and a manual value surviving learning.

## Not in this proposal

- Watering forecasts ("water in about N days"), which the dry-down rate enables.
- Learning from the air sensor or the season directly, beyond what the rolling
  average absorbs.
- Changing the probe's own moisture calibration; the thresholds absorb it.
