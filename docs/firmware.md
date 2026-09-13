# Firmware development

## Prerequisites

- ESP-IDF 5.2 or newer with the ESP32-C3 tools installed.
- A data-capable USB-C cable.
- A C compiler for host tests (`cc` on macOS/Linux).

The repository scripts automatically activate an existing ESP-IDF shell or the
default installation at `~/esp/esp-idf`. If ESP-IDF is unavailable, they print
the tested v5.5.5 installation commands. Set `IDF_PATH` to use another location.

## Build and flash

From the repository root:

```sh
sensor/firmware/build.sh
sensor/firmware/flush.sh
```

`build.sh` selects `esp32c3` when needed and builds the project. `flush.sh` runs
that build, detects one `/dev/cu.usbmodem*`, `/dev/cu.usbserial*`,
`/dev/ttyACM*`, or `/dev/ttyUSB*` device, flashes it, and opens the serial
monitor. Useful alternatives are:

```sh
sensor/firmware/flush.sh --port /dev/cu.usbmodem14801
sensor/firmware/flush.sh --no-monitor
```

Exit the monitor with `Ctrl-]`. Do not attach the probe or battery during the
first USB-only smoke test. Treat these scripts as the canonical workflow for
humans, CI helpers, and AI coding sessions; use raw `idf.py` only for diagnostics
or advanced configuration such as `idf.py menuconfig`.

### Firmware version

The authoritative firmware version is stored in `sensor/version.txt` and follows
[Semantic Versioning](https://semver.org/). Update that file before building a
release. ESP-IDF embeds its value in the application descriptor, boot output,
and firmware image. The `/status` response exposes it as `firmware_version`; the
web console shows the same compiled value in its browser title and header
subtitle. Versions are therefore tied to the binary rather than duplicated in
frontend source.

## Configuration

Project settings appear under **Open Plant Pulse** in `menuconfig`. Defaults are
development values, not proof of a sensor's register map:

| Setting | Default | Meaning |
| --- | --- | --- |
| `OPP_WEB_UI_ENABLED` | enabled | Development Wi-Fi log webpage |
| `OPP_WIFI_SSID` | empty | 2.4 GHz Wi-Fi network name |
| `OPP_WIFI_PASSWORD` | empty | Wi-Fi password stored in ignored `sdkconfig` |
| `OPP_SENSOR_MODBUS_ADDRESS` | 1 | Probe RTU slave address |
| `OPP_SAMPLE_INTERVAL_MINUTES` | 30 | Delay between wake cycles |

### Web UI diagnostics

Configure the development network locally without committing credentials:

```sh
source sensor/firmware/idf-env.sh
cd sensor
idf.py menuconfig
```

Under **Open Plant Pulse**, set the Wi-Fi SSID and password, then build and flash
with the canonical scripts. The serial log prints the DHCP address after the
ESP32-C3 connects. Open `http://<board-ip>/` for the live console,
`http://<board-ip>/logs` for plain text, or `http://<board-ip>/status` for JSON.
The status document includes the compiled `firmware_version`. The page mirrors
the newest 16 KiB of logs while preserving USB serial output.

The Maintenance view can persist the website color mode, accent, density, and
Overview card order, visibility, and width. Configuration is stored as a
versioned, validated JSON document in the `web_ui` NVS namespace, so it survives
power loss and firmware flashing unless NVS is explicitly erased. The current
API is:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/config/ui` | Read the active UI configuration |
| `PUT` | `/api/v1/config/ui` | Validate and persist a complete JSON configuration |
| `POST` | `/api/v1/config/ui/reset` | Erase the saved value and restore firmware defaults |
| `POST` | `/api/v1/restart` | Schedule a device restart after validating `{"confirm":"restart"}` |

Request bodies are limited to 1535 bytes. Unknown fields, duplicate or unknown
card IDs, unsupported values, invalid spans, and configurations with no visible
cards are rejected. The page also supports JSON import and export. This data is
small enough for NVS; a filesystem should be introduced only if future UI data
grows beyond bounded settings into larger user-authored documents.

The Maintenance view requires browser confirmation before it calls the restart
endpoint. A valid request receives `202 Accepted`; the response is sent before a
750 ms one-shot timer invokes `esp_restart()`. Restart does not erase NVS. This
development endpoint has no authentication and must only be exposed on a trusted
bench network. Restart request bodies are limited to 63 bytes.

Wi-Fi is intended for powered bench diagnostics. Disable `OPP_WEB_UI_ENABLED`
before battery-life testing or production deep-sleep builds.

UART pins, SHT45 I2C settings, register addresses, stabilization delay, and
power-enable polarity will be added only after the hardware checklist records
verified values.

The selected soil probe is the seven-parameter ComWinTop `NPKPHCTH-S`. The
existing parser implements only the candidate moisture, soil-temperature, and
conductivity register sequence. Add pH and N/P/K only after the exact purchased
revision's register addresses, types, scaling, ranges, and response lengths are
captured in fixtures and tests. Never infer register order solely from the model
name or another vendor's compatible-looking manual.

## Planned SHT45 integration

The SHT45 uses I2C and reports ambient air temperature plus relative humidity.
The firmware must use the sensor's CRC-protected commands, bound every operation
with a timeout, and distinguish `air_temperature` from the probe's
`soil_temperature` throughout internal types and logs. D4/GPIO6 SDA and
D5/GPIO7 SCL are candidate pins pending assembled-hardware verification.

Use the [XIAO ESP32-C3 and SHT45 first-node guide](sht45-first-node.md) for
wiring, measurement commands, CRC handling, BLE bring-up, and the required hub
changes for an air-only prototype.

Sampling frequency should remain low enough to avoid self-heating. The failure
policy must state whether a cycle with valid probe data but a failed SHT45 read
is omitted entirely or advertised with only valid fields; stale values are not
allowed.

## Planned deep-sleep lifecycle

The ESP32-C3 will use a timer wake-up to run one bounded acquisition and BLE
advertising cycle, then return to deep sleep. Probe power must be disabled on
every normal and error path before sleep begins. See
[sensor power management](power-management.md) for the power-domain model,
RTC timer behavior, peripheral shutdown requirements, and battery-life method.

## Host tests

The protocol modules do not require ESP-IDF. From the repository root:

```sh
sh scripts/test-sensor.sh
```

The script builds with C11 and treats all warnings as errors. Tests cover the
known Modbus request CRC, valid and corrupted responses, signed/scaled values,
and the exact BTHome service-data bytes.

## Implementation status

The current target application provides a development Wi-Fi log console but no
sensor acquisition yet. Add target code in this order: SHT45 and UART reads with
fixture data, GPIO power control, BLE advertisement lifecycle, guaranteed
cleanup, then deep sleep. See [the roadmap](roadmap.md) for acceptance criteria.