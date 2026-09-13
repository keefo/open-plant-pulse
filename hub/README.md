# Open Plant Pulse Hub

The hub is the always-on desktop/server side of Open Plant Pulse. It receives
BTHome v2 advertisements over the host's native Bluetooth stack, stores local
history, derives plant-state signals, and exposes those results to user
interfaces and home-automation systems.

## Supported hosts

- macOS through CoreBluetooth.
- Linux through BlueZ.

Python and [Bleak](https://github.com/hbldh/bleak) provide the shared BLE layer.
Platform service packaging belongs under the repository-level `deploy/` tree.

## Package boundaries

```text
hub/
├── src/open_plant_pulse_hub/
│   ├── domain/       Stable telemetry and plant models
│   └── ingestion/    BTHome decoding and future BLE collection
├── tests/            Hub unit and integration tests
└── pyproject.toml    Package metadata and dependency groups
```

Future adapters for SQLite, REST/WebSocket, MQTT, notifications, and prediction
must depend on the domain layer, not the reverse. macOS menu-bar UI code should
be a separate client of the local API rather than part of the BLE daemon.

## Development

Run the development hub from the repository root:

```sh
PYTHONPATH=hub/src python3 -m open_plant_pulse_hub
```

Then open <http://127.0.0.1:8080>. In a second terminal, start the simulated
sensor:

```sh
python3 simulator/sensor.py --time-scale 60
```

The hub accepts simulation datagrams only on loopback by default. Readings are
kept in memory and reset when the hub stops.

The tests use only the Python standard library:

```sh
PYTHONPATH=hub/src python3 -m unittest discover -s hub/tests
```

To prepare an isolated development environment later:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e './hub[api,mqtt,dev]'
```

## Current status

The development hub receives full simulated readings over UDP, persists unique raw
readings and care events indefinitely in SQLite, restores a short live history after
restart, serves JSON endpoints, and renders a local dashboard. File databases use
WAL mode and a versioned schema. BTHome decoding is tested, but real BLE scanning,
plant identity mapping, persistent date-range APIs, predictive analytics, backups,
and service installation remain roadmap work.

## Plant profile catalog

Plant profiles are JSON data under
`src/open_plant_pulse_hub/data/plant_profiles/`. Add one file following the
directory README and schema; the generic loader discovers and validates it
without any Python registry change.