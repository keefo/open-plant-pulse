# AI repository instructions

## Occam's Razor

Entities are not to be multiplied beyond necessity. Prefer the smallest
design that accounts for the facts, and make anything extra earn its
place before it is added.

- **One way to say a thing.** Where two states, flags, columns or verbs
  can mean the same situation, the second one is a bug waiting for
  somebody to pick the wrong one. `archived` came to mean both "I told
  this device to leave" and "this is the neighbour's"; afterwards
  nothing could tell them apart.
- **Do not add a switch for a decision already made.** A
  `--zigbee-backend` flag to choose between two radios is a choice
  nobody wanted to keep making. If the project has settled on one, wire
  it in and delete the other.
- **Fewer sources of truth.** A figure with three fallbacks has three
  ways to be surprising. The daily light reading once preferred a logged
  day, then typed hours, then a part of today -- and the part-day made a
  newly linked lamp report a tenth of its real light.
- **Replace rather than accumulate.** This project keeps no v1/v2 paths.
  When a format, backend or contract changes, the old one goes once the
  new one is proven, in the same breath rather than a release later.
- **Delete what nothing calls.** Dead code is not free: it is read,
  maintained and trusted by the next person to look.

The test before adding anything: *what fact forces this to exist?* If the
answer is a situation nobody has had, leave it out and add it when they
do. If the answer is "it might be useful later", that is not a fact.

Simplicity is not the same as doing less. A single rule that is honest
about a hard case beats two rules that each handle half of it, and
removing a distinction that is really there is not simplification --
it is a bug with a tidy diff.

## Show what was measured

If a measurement reaches the hub, the page for that device shows it,
and shows its history. This is not a per-device decision and not a
feature to add one sensor at a time.

- **The record decides, not the live value.** A chart appears when
  something has been kept worth drawing, not when the device happens to
  be saying it this minute. A sensor reporting its battery twice a day
  showed no battery history for the other eleven hours, while the
  history sat in the database.
- **The record decides, not the device's kind.** A battery chart
  belongs to anything with a battery, a temperature chart to anything
  that measures temperature. Asking "is this a thermometer?" to decide
  what to draw means every new kind of device arrives with its charts
  missing until somebody notices.
- **A figure that does not apply is absent, not a dash.** A mains plug
  has no battery and a soil probe no humidity; `--` reads as a reading
  that failed rather than one that was never going to exist.
- **Say when a device reports nothing yet**, rather than drawing an
  empty frame. "Nothing reported yet" and "this device does not measure
  that" are different sentences.

The test: a new device that reports a known measurement should get its
chart without anybody editing the page. If it would not, the drawing is
keyed to the wrong thing.

## A chart can be asked

Every line on a chart answers the pointer. Hovering it gives the
reading and when it was taken; on a chart with more than one line, it
gives the sensor's name too, because "24.9" is half an answer when two
lines are drawn.

- **The pointer finds the nearest reading, not only the dots.** A line
  drawn from hundreds of readings has dots too close together to aim
  at, and giving each a hit target is hundreds of elements rebuilt on
  every poll. One transparent sheet and a search is the same job at any
  density.
- **Touch counts.** There is no hover on a phone, and a number nobody
  can reach is worse than no number, so the same readout appears on a
  tap.
- **A colour means a sensor**, and keeps meaning it: derived from the
  sensor rather than from its position in a list, or the lines swap
  colours whenever a sensor reports and the legend has to be read
  again.

A chart nobody can question is a picture. This applies to any chart
with a line in it, not only the ones somebody has complained about.

## Definition of done

- Do not claim a task is complete while any requested step is unperformed, failed,
  or unverified.
- Treat version bumps, builds, flashes, device restarts, and physical-hardware checks
  as part of the task when the request or repository workflow requires them.
- For sensor firmware releases, update `sensors/soil-probe/version.txt` before building, use
  `sensors/soil-probe/firmware/build.sh` and `sensors/soil-probe/firmware/flush.sh`, and verify the running
  device reports the new version when hardware is available.
- Deploy sensor firmware either over USB-C with `sensors/soil-probe/firmware/flush.sh`, or
  over the air by uploading the built image to the hub (`POST /api/firmware`)
  and requesting the install (`POST /api/sensors/<id>/firmware`). Either way,
  confirm the sensor then reports the new version. Give every deployed binary a
  new version number: the hub judges an over-the-air install by the version the
  sensor reports back.
- Report each skipped or blocked step plainly. Distinguish code completion, build
  success, flash success, and hardware validation; none implies the others.
- Never describe a build, flash, test, or hardware behavior as successful without
  captured evidence from the corresponding command or device.

## Validation

- Follow the canonical commands documented in `README.md` and `docs/firmware.md`.
- Run relevant host checks before target deployment and review the final diff and
  repository status afterward.
- Preserve unrelated work already present in the working tree.
