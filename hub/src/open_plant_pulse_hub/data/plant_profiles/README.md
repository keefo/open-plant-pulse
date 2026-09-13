# Plant profiles

Plant knowledge is data, not Python code. Each public profile lives in its own
JSON file so a contribution can add or improve one plant without modifying the
hub loader.

## Add a profile

1. Copy the closest existing profile to `<profile_id>.json`.
2. Set `id` to the filename without `.json`; use lowercase letters, numbers,
   and underscores.
3. Keep `schema_version` at `1` and retain the `$schema` reference for editor
   validation.
4. Supply sources and reasoning in the pull request. Profile values are broad
   starter guidance, not laboratory or fertilization instructions.
5. Run `sh scripts/check.sh`. The loader discovers the file automatically; no
   Python registry edit is required.

`_catalog.json` contains catalog-wide display guidance, the default profile,
and whole-plant status levels. Files beginning with `_` are metadata and are
not loaded as plant profiles.

## Watering fields

- `refill_below`: point where watering becomes appropriate.
- `post_water_target`: desired range immediately after watering.
- `comfortable_cycle`: normal wet-to-dry movement between waterings.
- `concern_below` / `concern_above`: outer safety limits.

Thresholds use the sensor's calibrated 0–100 moisture index, not volumetric
water content. Pot, substrate, placement, and individual plant history can
require instance-specific adjustments in a future plant registry.

## Drainage fields

- `preferred_response`: ideal learned pot response: `fast`, `balanced`, or
   `retaining`.
- `acceptable_responses`: response classes considered suitable for the plant.
- `maximum_settle_minutes`: longest desired post-watering stabilization time.
- `label`: concise human-readable substrate and drainage guidance.

The hub learns response from the moisture peak and stable plateau after
watering. This measures retention and redistribution near the probe, not water
leaving a drainage hole. Suitability remains in a learning state until at least
three completed watering cycles are available.