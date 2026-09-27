# AI repository instructions

## Definition of done

- Do not claim a task is complete while any requested step is unperformed, failed,
  or unverified.
- Treat version bumps, builds, flashes, device restarts, and physical-hardware checks
  as part of the task when the request or repository workflow requires them.
- For sensor firmware releases, update `sensor/version.txt` before building, use
  `sensor/firmware/build.sh` and `sensor/firmware/flush.sh`, and verify the running
  device reports the new version when hardware is available.
- Deploy sensor firmware either over USB-C with `sensor/firmware/flush.sh`, or
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
