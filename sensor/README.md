# Open Plant Pulse sensor

The sensor is the battery-powered product installed at the plant. It reads a
ComWinTop NPKPHCTH-S soil probe over Modbus RTU and an SHT45 air sensor over I2C,
encodes implemented measurements as BTHome v2, briefly advertises over BLE, and
returns to deep sleep. The probe supports N/P/K, pH, conductivity, soil
temperature, and soil moisture; firmware currently implements only the latter
three.

```text
sensor/
├── firmware/          ESP-IDF main component and reusable protocol components
├── hardware/          Top-level assembly BOM and hardware guidance
├── tests/             Dependency-free C protocol tests
├── CMakeLists.txt      ESP-IDF project entry point
└── sdkconfig.defaults Portable target defaults
```

Read the [firmware guide](../docs/firmware.md),
[hardware guide](hardware/README.md), and
[enclosure workspace](hardware/enclosure/README.md), and
[shared radio contract](../protocol/README.md) before making changes.

From the repository root, use the canonical scripts:

```sh
sensor/firmware/build.sh
sensor/firmware/flush.sh
```

The flash script builds first, auto-detects a single USB serial port, flashes,
and opens the monitor. Run `sensor/firmware/flush.sh --help` for explicit port
and flash-only options. Reuse these scripts in development and automation rather
than assembling separate `idf.py` command sequences.

Run sensor protocol tests from the repository root:

```sh
sh scripts/test-sensor.sh
```