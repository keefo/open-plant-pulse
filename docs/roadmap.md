# Roadmap

## M0: Monorepo foundation

- [x] Separate sensor and hub product boundaries.
- [x] Host-tested Modbus parser and BTHome encoder.
- [x] Host-tested hub decoder with a shared protocol fixture.
- [x] Runnable simulated sensor, UDP ingestion, local API, and live dashboard.
- [x] Machine-readable planning BOM and maintenance policy.
- [x] System architecture, development, safety, and integration documentation.
- [x] Repository checks in continuous integration.

## M1: Sensor bench prototype

- [ ] Resolve exact MPNs for controller, RS485 interface, SHT45, and power parts.
- [ ] Capture the selected NPKPHCTH-S revision and verify all seven register mappings.
- [ ] Verify expansion-board power-enable behavior.
- [ ] Validate SHT45 I2C wiring, CRC-checked reads, placement, and thermal error.
- [ ] Implement bounded UART transport and configurable register mapping.
- [ ] Validate 100 consecutive reads against a reference Modbus tool.
- [ ] Implement NimBLE advertisement lifecycle and guaranteed power cleanup.

**Exit:** serial readings match the reference tool, the shared fixture is emitted,
and every error path deasserts probe power.

## M2: End-to-end hub ingestion

- [ ] Capture service UUID `0xFCD2` using Bleak on macOS and Linux.
- [ ] Define stable cross-platform sensor enrollment and identity.
- [ ] Add a versioned SQLite schema and migration mechanism.
- [ ] Persist valid readings with UTC receipt time and raw payload.
- [ ] Deduplicate repeated advertisements and expose collector health.

**Exit:** a 24-hour hardware run stores complete, queryable readings on both
supported platforms without duplicates or daemon crashes.

## M3: Local service and integrations

- [x] Persist deduplicated raw readings indefinitely in versioned SQLite storage.
- [ ] Add read-only health, sensor, latest-reading, and history APIs.
- [ ] Stream new readings over a local WebSocket.
- [ ] Add MQTT discovery/state publishing for Home Assistant.
- [ ] Add native notifications behind a platform adapter.
- [ ] Package launchd and systemd services with upgrade/rollback instructions.

**Exit:** a clean host install survives reboot and continues collecting when any
optional client, broker, or integration is offline.

## M4: Analysis and prediction

- [x] Detect and persist watering and possible fertilizing events from correlated sensor changes.
- [ ] Establish per-plant wet/dry baselines and data-quality rules.
- [ ] Calculate moisture slope and bounded depletion estimates.
- [ ] Model EC behavior relative to watering events and moisture changes.
- [ ] Calculate VPD from local SHT45 air temperature/humidity with documented
	canopy-temperature assumptions.
- [ ] Backtest predictions and report confidence/error rather than false precision.

**Exit:** algorithms have documented inputs, failure modes, and backtest results
against retained data before they drive user notifications.

## M5: User experience and hardware release

- [ ] Build a local web dashboard against the public hub API.
- [ ] Evaluate a separate macOS menu-bar client.
- [ ] Publish editable enclosure CAD, drawings, assembly photos, and frozen BOM.
- [ ] Complete measured battery-life and two-week hardware soak reports.
- [ ] Tag the first reproducible sensor-plus-hub release.

## Later: Actuation

Automated watering is intentionally outside the monitoring milestones. It needs
an explicit threat and hazard analysis, independent maximum-run protection,
manual override, leak detection, and fail-closed behavior before implementation.