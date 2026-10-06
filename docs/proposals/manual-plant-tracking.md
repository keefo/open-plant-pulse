# Plants without a sensor

## Status

Phases 1 to 4 built on 2026-10-05: the plant is the subject of its own
history, a plant can be held with no sensor, it has its own page, and the pot
can be weighed. Phase 5, unifying the routes, is not started, so an
instrumented plant is still at `/sensors/<id>` and a manual one at
`/plants/<id>`.

## Goal

Keep a plant in the hub that has no sensor on it: name it, give it a room and a
profile, record what you did to it, and track its weight — which for a potted
plant is the most honest measurement of water there is.

Most households have more plants than sensors. A hub that can only hold the
instrumented ones is a hub that shows you a corner of your own windowsill.

## What is in the way

The plant is already a real thing in the database — `plants` has identity, name,
profile and room, and a plant's lights and daily window hang off `plant_id`. But
everything else hangs off `sensor_id`: the care log, the journey figures, the
pages at `/sensors/<id>`. A plant with no sensor has nothing to hang from.

| Today keyed by | Tables |
| --- | --- |
| `plant_id` | `plant_lights`, `plant_light_schedules` |
| `sensor_id` | `care_events`, `plant_journey`, `sensor_readings`, the routes |

## Key decision

**The plant is the subject. A sensor is an instrument attached to it.**

A watering happened to a plant; a sensor is only how the hub came to know. So a
care event carries a `plant_id`, and a `sensor_id` as provenance when one
noticed it. The journey is the plant's. Readings stay the sensor's, because
that is genuinely what they are.

The alternative was a `sensors` row with `transport = 'manual'` — a sensor that
is not a sensor, carrying firmware, Wi-Fi, onboarding and interval columns that
mean nothing, with every BLE, inbox and fleet path taught to skip it. It is less
work this week and a tax on every change afterwards.

## Weight is the feature, not a side dish

For a pot, mass is the one measurement with no soil-type assumption and no
calibration: water in, water out, in grams. The probe reads 2 cm around its
needles; a scale reads the whole pot.

Three numbers per plant:

| Number | Where it comes from | What it is for |
| --- | --- | --- |
| **Watered weight** | recorded once drainage stops | the ceiling: a full pot |
| **Current weight** | entered when you lift it, or read from a scale | where it is now |
| **Thirsty weight** | recorded when the plant wanted water | the floor, learned over cycles |

From those: water used since watering (`ceiling − current`, grams ≈ millilitres),
how much of the pot's capacity is left, and — once two cycles are on record —
the average daily use, which turns into *water in about three days* rather than
a percentage nobody can act on.

The ceiling is set by a button on the plant's page, not typed into a settings
form: you weigh the pot when it has finished draining and press **This is the
watered weight**. The floor is offered the same way the next time you water,
from the weight recorded just before.

Weights are a time series, not a pair of settings, so the ceiling can be
re-recorded when the plant is repotted or grows, and the history stays.

**Stored in grams, entered in kilograms.** A 17 kg pot typed as `17.4` is
kinder than `17400`, and a seedling on a kitchen scale is still exact to the
gram underneath.

## What the page shows

One page for every plant, at `/plants/<plant_id>`, with less on it when there is
less to show. Not a second page that drifts from the first.

A plant with no sensor shows: its name and room; the weight panel and its
three numbers; **Watered today** / **Fertilized today** / **Add a note**;
its lights, if it has any; the care log; and the journey. No readings, no
charts, no pot response, no firmware, no raw reports — those sections simply
have nothing to render and stay absent.

A plant with a sensor shows all of that plus what it measures, and keeps its
`/sensors/<id>` address for now — unifying the two is phase 5, deliberately
last, because it touches every existing link and nothing else waits on it.

**No plant profile without a sensor.** A profile carries thresholds for
readings a manual plant will never have. What it otherwise gives is a species
name and a watering cadence, and both belong on the plant itself:
`expected_watering_days` covers the cadence until there is enough weight
history to do better.

## Schema

```sql
-- Which plant, and which instrument noticed, if any.
ALTER TABLE care_events ADD COLUMN plant_id INTEGER REFERENCES plants(plant_id);
ALTER TABLE plant_journey ADD COLUMN plant_id INTEGER REFERENCES plants(plant_id);

-- Every weighing, kept. source: 'manual' now, 'scale' when a load cell arrives.
CREATE TABLE plant_weights (
    weight_id INTEGER PRIMARY KEY,
    plant_id INTEGER NOT NULL REFERENCES plants(plant_id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    grams INTEGER NOT NULL CHECK (grams > 0),
    source TEXT NOT NULL CHECK (source IN ('manual', 'scale')),
    kind TEXT NOT NULL CHECK (kind IN ('reading', 'watered', 'thirsty')),
    note TEXT
);

-- A plant that exists on its own: no sensor, and a cadence to fall back on
-- when there are not yet two weight cycles to learn from.
ALTER TABLE plants ADD COLUMN room_id INTEGER REFERENCES rooms(room_id);
ALTER TABLE plants ADD COLUMN created_at TEXT;
ALTER TABLE plants ADD COLUMN expected_watering_days INTEGER;
```

Existing care events and journey rows are backfilled through
`sensors.plant_id`, and the code reads `plant_id` from then on. `sensor_id`
stays on a care event, because knowing which instrument saw a watering is worth
keeping.

## Manual care

`record_manual_care` already exists for fertilizing, with one entry per day
however many times the button is pressed. Watering joins it under the same
rule — a plant is watered once on a day or not at all — and a free note joins
it without that rule, because two things worth writing down can happen on one
day.

A manual watering also closes the watering window the same way a detected one
does, so a manual plant's journey counts missed windows like any other.

## Plan

1. **The plant as subject.** `plant_id` on care events and the journey,
   backfilled; reads switch to it; `rebuild_journey` keyed by plant. No visible
   change, and the tests that already compare kept figures against a fresh
   count are what prove it.
2. **Plants that stand alone.** Create, name, place and archive a plant with no
   sensor. It appears on the household page beside the instrumented ones, with
   *watered 3 days ago* where the others show a reading.
3. **The page.** `/plants/<id>` for a plant with no sensor, sections present
   only when they have something to say.
4. **Weight.** The table, the three numbers, the buttons, and water used in
   millilitres. Watering due from weight where there is weight, from the
   cadence where there is not.
5. **Unify the routes.** Every plant at `/plants/<id>`, with `/sensors/<id>`
   redirecting. Deferred on purpose: it touches every existing link, and the
   four phases above do not wait on it.
6. **Later, not now.** A load cell writing `source = 'scale'` rows into the
   same table, which is why the table is not called `manual_weights`.

## Validation

- [ ] A plant can be created, lived with for a week and archived, with no
      sensor involved at any point.
- [ ] Every figure on an instrumented plant's page is unchanged after the
      re-keying, proven by the kept-equals-rebuilt test on real history.
- [ ] `/sensors/<id>` still reaches the right plant.
- [ ] Two weighings a week apart produce a believable daily water use, and a
      third contradicts it if the plant was moved into the sun.
- [ ] A manual watering closes a watering-due window.
- [ ] Nothing on the page claims a measurement the plant does not have.

## Decided

- **Grams stored, kilograms entered** (2026-10-05).
- **No profile on a plant without a sensor** — species and cadence live on the
  plant itself (2026-10-05).
- **Routes unify last**, as phase 5, rather than alongside the feature
  (2026-10-05).

## Not in this proposal

- Photographs, which is the other thing people want on a plant page.
- Reminders or notifications. The hub shows that water is due; it does not
  chase you.
- Bulk entry for somebody weighing eleven plants on a Sunday.
