# Open Plant Pulse Hub

The hub is the always-on desktop/server side of Open Plant Pulse. Its target design
subscribes to BTHome advertisements over BLE, stores local history, derives
plant-state signals, and hosts a local HTTP web application where household users
manage and monitor sensors.

## Target hosts

- macOS.
- Linux.

Both hosts run the same Python BLE, HTTP, and SQLite service. Native scanning and
physical BTHome reception have been exercised on macOS; Linux permissions remain to
be tested. LAN binding and firewall behavior apply to browser access, not sensor
ingestion. Platform service packaging belongs under the repository-level `deploy/`
tree.

## Package boundaries

```text
hub/
├── src/open_plant_pulse_hub/
│   ├── domain/       Stable telemetry and plant models
│   └── ingestion/    BTHome decode, Bleak scan, replay, and development adapters
├── tests/            Hub unit and integration tests
└── pyproject.toml    Package metadata and dependency groups
```

Adapters for Bluetooth, SQLite, household HTTP, and later MQTT, notifications,
prediction, and public APIs must depend on the domain layer, not the reverse.

## Development

Run the development hub from the repository root:

```sh
PYTHONPATH=hub/src python3 -m open_plant_pulse_hub
```

Then open the fleet at <http://127.0.0.1:8080>. Sensor monitoring is available at
`/sensors/<sensor-id>`, where that sensor's configuration is also managed. The hub
scans for BTHome advertisements as it runs, so a sensor in range appears under
Settings and can be added from there. Readings and care events persist in SQLite;
a short live history is restored after restart.

A second server, on port 8081, serves firmware images to sensors on the household
network; the management interface above stays on loopback. Add an image under
Settings · Firmware and install it from a sensor's configuration page. Run with
`--no-firmware-server` to keep images without serving them.

The tests use only the Python standard library:

```sh
PYTHONPATH=hub/src python3 -m unittest discover -s hub/tests
```

To prepare an isolated development environment later:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e './hub[dev]'
```

## Current status

The development hub receives BTHome v2 advertisements through Bleak, persists
unique readings and bounded receive
diagnostics in migrated SQLite storage, restores a short live history after
restart, serves JSON endpoints, and renders a local dashboard. The shared replay
path tests three stable sensor identities, duplicate suppression, partial sources,
malformed isolation, and adapter recovery. The production subscriber stored physical
BTHome reports on macOS, and direct CoreBluetooth access verified the firmware's
configuration write/read-back on 2026-09-14; Linux remains unvalidated.
The household UI provides a fleet page and per-sensor detail pages. Fleet cards open
the corresponding detail page and show name, stable identifier, last report, and
state. Each detail page includes all sensor configuration, archive/replacement,
destructive deletion, and pending/applied/retrying state for plant name, room, and
reporting interval. The requested hub interval remains editable while a separate
read-only field shows the interval last confirmed from firmware and used for hub
freshness calculations.
Each sensor detail page also includes a temporary, newest-first raw BTHome report
log for debugging, capped at the latest 50 reports.
The sensor inbox retains deliberate enrollment.
These flows and the connected-BLE codec/read-back synchronizer are integration-tested;
single-sensor physical configuration delivery is verified, while production-hub
delivery scheduling, multi-sensor radio behavior, and responsive-browser validation
remain pending. A stable public HTTP API, sensor
Wi-Fi/HTTP, MQTT, predictive analytics, backups, and service installation are lower
priority.

The canonical current and target design is in [`../docs/hub.md`](../docs/hub.md).

## Plant profile catalog

Plant profiles are JSON data under
`src/open_plant_pulse_hub/data/plant_profiles/`. Add one file following the
directory README and schema; the generic loader discovers and validates it
without any Python registry change.
