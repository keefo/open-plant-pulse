# Roadmap

## M0: Monorepo foundation

- [x] Separate sensor and hub product boundaries.
- [x] Host-test the Modbus parser and BTHome encoder/decoder.
- [x] Add a shared BTHome fixture.
- [x] Run BLE ingestion, the SQLite store, internal HTTP endpoints, and a live
  dashboard end to end.
- [x] Add system architecture, development, safety, and integration documentation.
- [x] Add repository checks in continuous integration.

## M1: Freeze the BLE ingestion contract

- [x] Define stable identity across macOS, Linux, hub replacement, and sensor reset.
- [x] Define a packet identifier or tested advertisement deduplication rule.
- [x] Finalize SHT45 object semantics and partial-source behavior.
- [x] Decide whether initial advertisements remain unencrypted and document the
  later key-provisioning path if encryption is required.
- [x] Add fixtures for multiple sensors, duplicates, partial samples, malformed
  payloads, and unsupported objects.
- [x] Add a deterministic advertisement replay adapter using the same application
  ingestion interface as the physical scanner.

**Exit:** at least three replayed sensors remain distinct, duplicates are not
stored, and a failed source cannot appear as a fresh value.

## M2: Hub BLE subscriber and durable storage

- [x] Implement continuous BTHome `0xFCD2` subscription with `bleak`.
- [x] Add explicit SQLite migrations for sensors, enrollment, advertisements,
  partial readings, and hub-owned plant metadata.
- [x] Persist readings and bounded receive diagnostics transactionally.
- [x] Recover with bounded backoff after adapter loss and isolate malformed devices.
- [x] Report scanner, database, and last-receive health independently.
- [ ] Validate native Bluetooth behavior and permissions on macOS and Linux (macOS
  scanner startup verified; physical BTHome reception and Linux remain).

**Exit:** physical or captured advertisements survive hub restart, duplicate
callbacks, adapter interruption, and concurrent traffic from at least three
sensors without crossing identities.

## M3: Household HTTP server and web application

- [x] Add hub health and an inbox for newly observed, unclaimed sensors.
- [x] Add multi-plant overview and per-sensor reading/history views.
- [x] Add name, room, plant/profile, threshold, archive, and replace/merge flows.
- [x] Link fleet cards to sensor-detail routes; consolidate configuration,
  rename/delete, and persisted per-sensor device configuration on each detail page.
- [x] Add host-tested connected-BLE delivery/read-back for plant name, room, and
  reporting interval, with NVS-backed firmware configuration.
- [x] Clearly distinguish fresh, stale, unavailable-source, and scanner-failure
  states.
- [x] Keep routes required by the web application internal while they stabilize.
- [x] Default to loopback and document explicit LAN binding, firewall, same-origin,
  and CSRF behavior.

**Exit:** a household user can enroll, manage, and monitor three BLE sensors from a
browser without a terminal, with device changes clearly shown as pending or applied.

## M4: Sensor bench and physical BLE lifecycle

- [ ] Resolve exact MPNs and verify the seven soil-probe register mappings.
- [ ] Validate SHT45 wiring, CRC-checked reads, placement, and thermal error.
- [ ] Implement bounded UART acquisition and guaranteed probe-power cleanup.
- [x] Implement a bounded BTHome advertise/stop/deep-sleep cycle (built for ESP32-C3;
  physical cycle validation remains below).
- [x] Add an always-awake development path that reports every five seconds before
  configuration and uses the hub-delivered interval afterward.
- [ ] Verify repeated delivery under normal and degraded radio conditions.
- [ ] Measure complete-cycle and deep-sleep energy at intended reporting intervals.
- [x] Run the canonical version bump, build, flash, and running-device verification
  for the firmware 0.3.0 always-awake bench image.
- [ ] Flash and verify the firmware 0.3.0 production BLE/deep-sleep image separately.

**Exit:** a 24-hour run stores queryable BLE samples, every error path deasserts
probe power, and build, flash, running version, BLE delivery, and power results are
recorded separately.

## M4.5: Firmware over the air

- [x] Keep built images on the hub, parsed and identified by what they contain.
- [x] Give the sensor two application slots without losing its bond.
- [x] Command an update over the bonded link and download it over the household network.
- [x] Verify the digest before an image is made bootable, and roll back one that
      does not start.
- [ ] Update several sensors, and a sensor that sleeps between reports.
- [ ] Sign images, which needs a key that outlives any one hub.

**Exit:** a sensor is updated from the browser with no cable, and a bad image
costs nothing. Reached on 2026-09-25 for one powered sensor; a sleeping sensor
and more than one at a time are untested.

## M5: Service packaging and resilience

- [ ] Package launchd and systemd services with native Bluetooth permissions.
  launchd is done (`deploy/launchd/`, 2026-09-27); systemd is not.
- [ ] Verify reboot startup, clean shutdown, database migration, backup, and rollback.
- [ ] Test explicit household LAN access and firewall instructions on both hosts.
- [ ] Add retention/export controls after representative database growth is measured.

**Exit:** a clean host install survives reboot and resumes collection without
manual terminal steps; the browser still serves stored history during BLE outages.

## M6: Analysis and integrations

- [x] Detect and persist watering and possible fertilizing events from correlated
  sensor changes.
- [ ] Establish per-plant baselines and data-quality rules.
- [ ] Calculate moisture slope, bounded depletion estimates, EC behavior, and VPD.
- [ ] Backtest predictions and report confidence/error rather than false precision.
- [ ] Add MQTT discovery/state publishing and native notifications behind adapters.

## Later: public API and alternate sensor transport

- [ ] Stabilize and version a read-only public HTTP API after the household UI and
  BLE domain model settle.
- [ ] Add authenticated management API operations only when needed.
- [ ] Measure complete BLE, HTTP, and authenticated/TLS HTTP wake-cycle energy under
  normal and degraded signal conditions.
- [ ] Re-evaluate Wi-Fi/HTTP or hybrid sensor transport for acknowledged delivery,
  larger diagnostics, or remote configuration.
- [ ] Evaluate a separate macOS menu-bar client only after a public API exists.

## Later: Actuation

Automated watering is intentionally outside the monitoring milestones. It needs
an explicit threat and hazard analysis, independent maximum-run protection,
manual override, leak detection, and fail-closed behavior before implementation.