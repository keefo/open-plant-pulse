# Open Plant Pulse

Open Plant Pulse is a local-first plant monitoring system with two products:

- A battery-powered **sensor node** reads a seven-parameter NPKPHCTH-S RS485 soil
   probe and an SHT45 ambient sensor, then broadcasts the implemented measurement
   subset using BTHome v2.
- An always-on **desktop hub** receives nearby sensors on macOS or Linux, stores
   history, derives plant-state insights, and serves local integrations.

The sensor can still be discovered directly by Home Assistant. The hub is for
users who want durable local history, analysis, native notifications, APIs, and
multi-system forwarding without putting those responsibilities on the battery
device.

> [!IMPORTANT]
> This repository is in the prototype stage. Protocol encode/decode logic is
> tested, but BLE collection, target hardware, persistence, predictions, and
> packaging have not yet been validated end to end.

## Repository layout

```text
open-plant-pulse/
├── sensor/                  Battery sensor product
│   ├── firmware/            ESP-IDF application and components
│   ├── hardware/            Assembly BOM, wiring, and future CAD/PCB assets
│   └── tests/               Host-side sensor protocol tests
├── hub/                     Installable Python desktop/server product
│   ├── src/                 Domain and adapter packages
│   └── tests/               Hub unit and integration tests
├── simulator/               Development sensor broadcasting full dummy readings
├── protocol/                Versioned sensor-to-hub contract and fixtures
├── deploy/                  Future launchd/systemd and packaging assets
├── docs/                    System architecture, setup, roadmap, and proposals
├── examples/                Downstream integration examples
└── scripts/                 Repository-wide validation commands
```

Product code stays inside `sensor/` or `hub/`. Data exchanged between them is
specified in `protocol/`; neither product may privately redefine that contract.

## Run the simulated system

Start the local hub from the repository root:

```sh
PYTHONPATH=hub/src python3 -m open_plant_pulse_hub
```

In another terminal, start the dummy sensor:

```sh
python3 simulator/sensor.py --time-scale 60
```

Open <http://127.0.0.1:8080> to view live soil, air, and nutrient readings. At the
default `60×` scale, every real second represents one simulated minute; pass
`--time-scale 1` to run against real time.

## Start here

1. Read [the system architecture](docs/architecture.md) and
   [shared protocol](protocol/README.md).
2. For hardware work, use the [sensor guide](sensor/hardware/README.md),
   [BOM](sensor/hardware/BOM.md), [enclosure workspace](sensor/hardware/enclosure/README.md),
   and [bring-up checklist](docs/bring-up.md).
3. To start with a XIAO ESP32-C3 and SHT45 only, follow the
   [first sensor-node guide](docs/sht45-first-node.md). For broader embedded
   development, use [the sensor firmware guide](docs/firmware.md).
4. For desktop development, follow [the hub guide](docs/hub.md).
5. Choose a direct or hub-mediated Home Assistant path in
   [the integration guide](docs/home-assistant.md).

## Repository checks

These checks require only a C compiler and Python 3.9 or newer:

```sh
sh scripts/check.sh
```

## Sensor firmware

Use the repository scripts for ESP32-C3 work. They locate and activate ESP-IDF,
select the correct project directory, and keep the commands reproducible:

```sh
sensor/firmware/build.sh
sensor/firmware/flush.sh
```

`flush.sh` builds, auto-detects a single USB serial device, flashes it, and opens
the monitor. Exit the monitor with `Ctrl-]`. Use `--no-monitor` for flash only or
`--port /dev/DEVICE` when multiple serial devices are connected. Future tooling
and automation should call these scripts instead of duplicating raw `idf.py`
commands.

## Current status

| Area | Status |
| --- | --- |
| Sensor Modbus parser and BTHome encoder | Host tested |
| Simulated sensor to hub to web dashboard | Runnable and integration tested |
| Hub BTHome decoder and shared fixture | Host tested; BLE scanner planned |
| ESP-IDF target application | Scaffolded; hardware transport planned |
| Hub BLE scanner and SQLite persistence | Planned |
| Hub API, UI, MQTT, notifications, predictions | Planned |
| Electrical design and enclosure | Needs prototype validation |

See [the roadmap](docs/roadmap.md) for acceptance criteria. The original Chinese
[sensor](docs/proposals/sensor-node.zh-CN.md) and
[hub](docs/proposals/desktop-hub.zh-CN.md) brainstorms are retained as proposals,
not specifications. The project is licensed under the [MIT License](LICENSE).