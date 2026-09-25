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
| `OPP_PRODUCTION_LIFECYCLE` | disabled | One-shot SHT45/BTHome/deep-sleep mode; bypasses development services |
| `OPP_BTHOME_ADVERTISEMENT_WINDOW_MS` | `3000` | Fixed advertising window in production and development modes |
| `OPP_BTHOME_ADVERTISEMENT_INTERVAL_MS` | `250` | Advertising interval within each bounded window |
| `OPP_WIFI_SSID` | empty | 2.4 GHz Wi-Fi network name |
| `OPP_WIFI_PASSWORD` | empty | Wi-Fi password stored in ignored `sdkconfig` |
| `OPP_CLOCK_SYNC_ENABLED` | enabled | Validate and synchronize the system clock over SNTP |
| `OPP_SNTP_SERVER` | `pool.ntp.org` | SNTP server used after DHCP succeeds |
| `OPP_CLOCK_SYNC_TIMEOUT_SECONDS` | 15 | Maximum duration of one SNTP attempt |
| `OPP_CLOCK_WIFI_WINDOW_SECONDS` | 30 | Total clock-only Wi-Fi connection window |
| `OPP_CLOCK_SYNC_RETRY_MINUTES` | 15 | Delay after a failed SNTP attempt |
| `OPP_CLOCK_RESYNC_INTERVAL_HOURS` | 12 | Opportunistic RTC drift-correction interval (6–24 hours) |
| `OPP_CLOCK_JUMP_WARNING_SECONDS` | 300 | Log threshold for a forward or backward correction |
| `OPP_SHT45_SDA_GPIO` | 6 | SHT45 I2C data pin (XIAO D4) |
| `OPP_SHT45_SCL_GPIO` | 7 | SHT45 I2C clock pin (XIAO D5) |
| `OPP_SHT45_SAMPLE_INTERVAL_SECONDS` | 5 | Awake development sampling interval |
| `OPP_SENSOR_MODBUS_ADDRESS` | 1 | Probe RTU slave address |
| `OPP_SAMPLE_INTERVAL_MINUTES` | 30 | Initial delay before hub device configuration is applied |

### Partitions and over-the-air updates

`sensor/partitions.csv` gives the board two 1600 KiB application slots inside its
4 MB of flash, with `nvs` at the offset and size it has always had:

```
nvs        0x9000    24K
otadata    0xf000     8K
phy_init   0x11000    4K
ota_0      0x20000  1600K
ota_1      0x1b0000 1600K
```

Because `nvs` did not move, the one cable flash that introduces this layout keeps
a sensor's bond, pairing code and configuration. Firmware from before 0.12.0 has
a single slot and cannot install anything over the air, so a sensor running one
needs that flash by cable. Everything after it is installed from the hub: see
[the proposal](proposals/firmware-over-the-air.md) for how, and `sensor/version.txt`
for what is running.

A newly installed image boots on trial. It is kept once it has advertised, which
is what the sensor is for; one that crashes before that is replaced by its
predecessor at the next reset, with no help from anybody.

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
The status document includes the compiled `firmware_version`, Wi-Fi SSID, IP and
station MAC, inferred power source, clock/RTC state, SHT45 enabled state and
availability, latest ambient values, sample sequence, timestamp validity, and
sample age. The Overview ESP32-C3 section shows the SSID, IP, and MAC together
on the Device card, plus power, UTC date/time, and RTC synchronization details.
The page mirrors the newest 16 KiB of logs while preserving USB serial output.

### Power source inference

`/status` exposes `usb_connected` and `power_source`. The firmware reports `usb`
when the USB Serial/JTAG peripheral receives host SOF packets; otherwise it reports
`battery_inferred`. The Overview Power card displays these states as `USB` and
`Battery (inferred)`. USB takes precedence when USB and battery are both attached.

This inference assumes the node is not powered by a USB charger or power bank,
because those supplies do not provide the USB host traffic used for detection. It
does not measure battery level or charging state; those require additional hardware.

### Clock and sample timestamps

Firmware reads wall-clock time through `time()`/`gettimeofday()`. A Unix time from
2024-01-01 through 2099-12-31 is considered plausible. The ESP32-C3 RTC-backed
system clock and the last-successful-sync marker are retained through deep sleep;
power loss clears the marker and normally leaves the clock invalid.

After Wi-Fi receives an address, an invalid clock or a clock last synchronized at
least 12 hours ago starts one bounded SNTP attempt. With the web UI disabled, the
firmware skips Wi-Fi entirely while the clock is current and limits a required
clock-only Wi-Fi session to 30 seconds by default. Failed attempts do not block
sampling and are retried after the configured delay on an available Wi-Fi
session or later wake. A correction records and logs the measured clock
adjustment; existing sample timestamps are never rewritten.

Each SHT45 sample receives a retained sequence number and monotonic offset. It
also receives an immutable UTC timestamp only when the wall clock is plausible at
acquisition time. `/status` exposes `air_sample_sequence`,
`air_sample_monotonic_ms`, and nullable `air_sample_time_utc`, allowing a consumer
to retain and order samples collected before synchronization. Persistent sensor
history is intentionally not added here because durable history remains owned by
the hub. When valid, `air_sample_unix_ms` preserves the full acquisition
timestamp represented by `air_sample_time_utc`.

The console has Overview and Maintenance pages. Overview presents the ESP32-C3 and
Hub configuration in metric-card sections, with live ESP-IDF logs at the bottom.
The Sensors section on Maintenance can enable or disable each sensor. Maintenance
also persists the website color mode, accent, density, IANA timezone, `/status`
refresh interval, and Overview card order, visibility, and width. The refresh choices range
from one second to one hour, or Off; Off still permits the page's initial status
request. The Overview
clock and RTC synchronization timestamp are converted from device-supplied UTC
by the browser, including daylight-saving transitions for the selected timezone.
Existing configurations migrate to `UTC` with sensors enabled, the Power card
visible, and the prior two-second status refresh interval. Configuration is stored
as a versioned, validated JSON document in the
`web_ui` NVS namespace, so it survives power loss and firmware flashing unless
NVS is explicitly erased. Control changes are previewed immediately and saved
automatically after a short debounce.
The current API is:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/config/ui` | Read the active UI configuration |
| `PUT` | `/api/v1/config/ui` | Validate and persist a complete JSON configuration |
| `POST` | `/api/v1/config/ui/reset` | Erase the saved value and restore firmware defaults |
| `POST` | `/api/v1/reports/force` | Queue an immediate report and return its request ID |
| `POST` | `/api/v1/restart` | Schedule a device restart after validating `{"confirm":"restart"}` |

Request bodies are limited to 1535 bytes. Unknown fields, duplicate or unknown
card IDs, unsupported values, invalid timezone syntax, invalid spans, and
configurations with no visible cards are rejected. The timezone control lists
the IANA zones supported by the current browser. The page also supports JSON
import and export. This data is
small enough for NVS; a filesystem should be introduced only if future UI data
grows beyond bounded settings into larger user-authored documents.

The Maintenance view requires browser confirmation before it calls the restart
endpoint. A valid request receives `202 Accepted`; the response is sent before a
750 ms one-shot timer invokes `esp_restart()`. Restart does not erase NVS. This
development endpoint has no authentication and must only be exposed on a trusted
bench network. Restart request bodies are limited to 63 bytes.

The always-on Wi-Fi web UI is intended for powered bench diagnostics. Disable
`OPP_WEB_UI_ENABLED` before battery-life testing or production deep-sleep builds.
The production design uses one bounded BTHome advertising burst per wake. Sensor
Wi-Fi/HTTP reporting is lower-priority evaluation work and is not required by the
first hub.

UART pins, probe register addresses, stabilization delay, and power-enable
polarity will be added only after the hardware checklist records verified values.

The selected soil probe is the seven-parameter ComWinTop `NPKPHCTH-S`. The
existing parser implements only the candidate moisture, soil-temperature, and
conductivity register sequence. Add pH and N/P/K only after the exact purchased
revision's register addresses, types, scaling, ranges, and response lengths are
captured in fixtures and tests. Never infer register order solely from the model
name or another vendor's compatible-looking manual.

## SHT45 integration

The SHT45 driver uses I2C port 0 at address `0x44`, 100 kHz, and configurable
D4/GPIO6 SDA and D5/GPIO7 SCL defaults. It probes with a 100 ms timeout and uses
the high-precision, no-heater `0xFD` command. Both returned words must pass the
SHT4x CRC-8 check before the reading is published. Humidity is clamped to 0-100%.

The awake development firmware takes one-shot readings every five seconds. Valid
samples are logged as `air_temperature_c` and `air_humidity_percent`, exposed by
`/status`, and displayed in an automatic Overview section. The Sensors section on
Maintenance shows the SHT45 status badge and its persisted enable switch. The Overview derives
absolute humidity in g/m³ from each temperature and relative-humidity sample using
the Magnus saturation-vapor-pressure approximation.
The Overview section is hidden when the SHT45 has no valid reading. A failed
transaction immediately invalidates the latest sample so stale values are not
served. Probe failures are retried without preventing the Wi-Fi diagnostics
console from starting.

Disabling SHT45 sampling updates the versioned JSON configuration in NVS,
invalidates its latest reading, releases the I2C bus, and prevents further probe
and measurement transactions. Re-enabling it wakes the monitor immediately. This
reduces controller activity and avoids measurement energy, but it does not remove
power from a breakout wired directly to the always-on 3.3 V rail; eliminating its
standby current requires a verified switched sensor rail.

Use the [XIAO ESP32-C3 and SHT45 first-node guide](sht45-first-node.md) for
wiring, measurement commands, CRC handling, BLE advertisement bring-up, and the
required hub changes for an air-only prototype.

Sampling frequency should remain low enough to avoid self-heating. Production mode
publishes only a newly acquired SHT45 sample; a failed read skips advertising and
returns to deep sleep rather than reusing an earlier ambient measurement.

When `OPP_PRODUCTION_LIFECYCLE` is disabled, an unconfigured development image
remains awake and emits an air-only BTHome report every five seconds from the latest
valid SHT45 sample. Once the hub sends device configuration, this path uses the
persisted 1-second-to-24-hour reporting interval. The bounded advertisement window
and radio interval remain configurable in `menuconfig`; no deep-sleep API is called
on this path.

### Hub-delivered device configuration

Each bounded BTHome window is connectable and exposes the configuration service
documented in [`protocol/README.md`](../protocol/README.md). The hub writes plant
name, room, interval, and revision after the sensor reports, then reads the value
back. Firmware validates the complete payload, commits it to NVS, and changes the
next report schedule. Identical writes do not consume another NVS write.

`/status` exposes `device_config_revision`, `plant_name`, `room`, and
`reporting_interval_seconds`. The sensor Overview renders those values. Before the
first valid configuration, revision is zero and the compiled interval remains the
production fallback.

### Manual forced reports

The Maintenance **Report now** control is available only in the always-awake Wi-Fi
diagnostics runtime. It queues a fresh BLE report immediately, without changing or
resetting the configured periodic deadline. The page tracks the request ID and
BTHome packet ID through queued, reporting, acknowledged, unacknowledged, and failed
states. A successful result means the Hub durably accepted that exact packet and
wrote the matching acknowledgment token back during the same BLE window.

The request uses the standard BTHome button-press event and the connected-BLE
acknowledgment characteristic documented in [`protocol/README.md`](../protocol/README.md).
If there is no current SHT45 sample, advertising fails, the Hub is stopped, or the
token does not match, the dashboard shows failure rather than inferring delivery.
Production deep-sleep firmware intentionally has no web button and does not wait
for acknowledgments.

The service currently has no encryption, authentication, or physical-presence
gate. It is suitable only for prototype validation on a trusted bench.

## Production deep-sleep lifecycle

Enable `OPP_PRODUCTION_LIFECYCLE` to replace the powered diagnostics runtime with
one bounded wake cycle. Production firmware reads the SHT45 once, omits the complete air
object group if acquisition fails, advertises a fresh contract-v2 sample with a
deep-sleep-retained packet ID and eFuse-derived `sensor-<DEVICE_ID>` name, explicitly
stops/deinitializes NimBLE, and enters timer deep sleep. The advertisement window
defaults to 3000 ms at a 250 ms interval; the sleep interval defaults to 30 minutes
until hub configuration is applied.

This first implementation is air-only. The unverified soil UART and probe-power
hardware are deliberately not energized. Probe power must be disabled on every
normal and error path when that acquisition stage is added. See
[sensor power management](power-management.md) for the power-domain model,
RTC timer behavior, peripheral shutdown requirements, and battery-life method.

## Host tests

The protocol modules do not require ESP-IDF. From the repository root:

```sh
sh scripts/test-sensor.sh
```

The script builds with C11 and treats all warnings as errors. Tests cover the
known Modbus request CRC, valid and corrupted responses, signed/scaled values,
contract-v1 and contract-v2 BTHome service-data bytes, partial source omission,
packet IDs, and stable local-name formatting.

## Implementation status

The target application provides two compile-time modes: powered Wi-Fi diagnostics
and an opt-in bounded SHT45/BTHome/deep-sleep lifecycle. Firmware 0.4.0 was built,
flashed, and verified on the physical board in always-awake diagnostics mode. Direct
connected-BLE write/read-back, NVS restoration after reset, the live sensor dashboard,
and a 300-second configuration were verified with revision 4. Firmware 0.5.0 adds
the manual forced-report acknowledgment workflow; build, flash, and live validation
results are recorded in the worklog. The production lifecycle remains unflashed and unmeasured;
USB source detection is not a current or energy measurement, and the 24-hour soak is
still pending. Add remaining target code in this order: UART reads with fixture data,
verified GPIO power control, centralized probe cleanup, then physical repeated-cycle
and power validation. See [the roadmap](roadmap.md) for acceptance criteria.
