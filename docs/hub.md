# Desktop hub development

## Scope

The hub is one background service for macOS and Linux. Its first responsibility
is reliable collection and local persistence. Dashboards, predictions, MQTT, and
native clients are downstream features and must not block ingestion.

## Technology baseline

| Concern | Initial choice | Reason |
| --- | --- | --- |
| Runtime | Python 3.9+ and asyncio | Available on target hosts and supported by Bleak |
| BLE | Bleak | Shared CoreBluetooth and BlueZ abstraction |
| Store | SQLite via aiosqlite | Embedded, transactional, and low maintenance |
| Local API | Optional FastAPI extra | Typed REST/WebSocket interface when needed |
| Home Assistant bridge | Optional paho-mqtt extra | Standard MQTT discovery path |
| Tests | unittest baseline; pytest dev extra | Zero-dependency core tests and richer integration tests |

Dependencies are declared in [`hub/pyproject.toml`](../hub/pyproject.toml). Keep
optional outputs in extras so a headless collector does not require web or MQTT
packages.

## Development setup

From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e './hub[api,mqtt,dev]'
pytest hub/tests
```

The current contract tests need no installation:

```sh
PYTHONPATH=hub/src python3 -m unittest discover -s hub/tests
```

## Simulated vertical slice

No package installation is needed for the first runnable demo. Start the hub:

```sh
PYTHONPATH=hub/src python3 -m open_plant_pulse_hub
```

Start the dummy sensor in another terminal:

```sh
python3 simulator/sensor.py --time-scale 60
```

Visit <http://127.0.0.1:8080>. The simulator sends the full planned measurement
set through development-only UDP on `127.0.0.1:8765`; the hub validates each
envelope, persists it in SQLite, keeps the latest 180 readings in memory, and exposes:

- `GET /api/health`
- `GET /api/readings/latest`
- `GET /api/readings/history`
- `GET /api/care-log`
- `PUT /api/sensors/profile`

The UDP adapter is intentionally loopback-only and will be replaced by a Bleak
adapter for production sensor ingestion. Raw readings and detected care events
persist indefinitely in `~/.open-plant-pulse/hub.sqlite3`; pass `--database PATH`
to use another location. The file database uses WAL mode, has a versioned additive
schema, and ignores duplicate `(sensor_id, sequence, observed_at)` envelopes. The
history API remains a recent 180-reading live view; persistent date-range queries,
stable plant identity, scheduled backups, and migration tooling remain future work.
Care analysis uses a valid timezone-aware sensor `observed_at`; adapters without an
observation timestamp fall back to the real hub receipt time. This lets the simulator
accelerate time without changing production cooldown and settling rules. The web
service publishes its `started_at` through `/api/health`; this is simulation time zero,
so start the hub before the simulator. Dashboard sample times use `observed_at` and
therefore follow the same accelerated clock.

The care detector records a watering event when moisture rises at least 15 percentage
points between sequential readings. It records a possible fertilizing event when
conductivity rises by at least 250 µS/cm together with a material N, P, or K rise.
It also records `watering_due` once when moisture crosses downward through the
selected profile's `refill_below` marker. That alert re-arms only after moisture
recovers at least three points above the marker, avoiding repeated events from noise.
Watering is high-confidence evidence; fertilizing requires confirmation because
moisture and dissolved salts can also move conductivity. A 30-minute per-kind cooldown
prevents one application from producing repeated entries. Watering detection re-arms
early when moisture returns within five points of its pre-watering baseline, allowing
a distinct dry-back and watering cycle to be recorded during accelerated simulation.

After watering, the detector tracks the moisture peak for at least 10 minutes and
requires three readings within a 1.5-point range before treating the curve as settled.
It records baseline, peak, settled moisture, retained fraction, settle time, and a
`fast`, `balanced`, or `retaining` response class. The dashboard compares the latest
three completed cycles with the selected plant profile before judging pot suitability.
This estimates retention and redistribution around the probe; it does not prove runoff.

## Planned package layout

Add packages only with their first working behavior:

```text
open_plant_pulse_hub/
├── domain/          Models and plant-state rules
├── ingestion/       BTHome decoder and BLE collection
├── application/     Use cases and repository interfaces
├── adapters/        SQLite, MQTT, HTTP, weather, and OS implementations
└── cli.py            Service entry point and lifecycle
```

## Delivery order

1. Replace simulated UDP with BLE scan diagnostics using the same store.
2. Stable sensor enrollment and plant identity mapping.
3. Add scheduled SQLite backups and migration tooling.
4. Extend the current read-only API with persistent date-range history.
5. MQTT and native notification adapters.
6. Trend analysis with backtested, uncertainty-aware predictions.
7. Web and menu-bar clients against the same local API.

## Platform constraints

macOS requires Bluetooth permission for the process running the daemon. Linux
requires BlueZ and an explicit service/user permission strategy. Development
must test real advertisements on both platforms before claiming support.

Service definitions will live in [`deploy/`](../deploy/README.md); do not place
launchd or systemd lifecycle code in the Python domain package.