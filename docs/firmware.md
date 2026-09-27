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

In a terminal both scripts fold compile and flash output into one progress bar
and hide ESP-IDF's activation messages; warnings and errors still print in
full, and the complete output is kept in `sensor/build/build.log` and
`sensor/build/flash.log`. On failure the last 40 lines are shown. Set
`OPP_VERBOSE=1` for the full output in the terminal. When output is not a
terminal (CI, a pipe, a log file) it is printed unchanged.

Builds are deployed over USB-C with `flush.sh`, or over the air through the
hub: upload the built `sensor/build/open_plant_pulse.bin` in Settings ->
Firmware (or `POST /api/firmware`) and install it from the sensor's page (or
`POST /api/sensors/<id>/firmware` with its digest). Over the air needs the
sensor advertising, which development builds do continuously
(`OPP_ALWAYS_REACHABLE`); it cannot recover a sensor that no longer boots, which
still needs the cable. Give each deployed binary a new version, since the hub
confirms an install by the version the sensor reports.

Exit the monitor with `Ctrl-]`. Do not attach the probe or battery during the
first USB-only smoke test. Once the probe is attached, fit the battery before
testing it: USB alone cannot power the probe (see
[the hardware notes](../sensor/hardware/README.md#the-node-must-have-its-battery-in)).

If no serial port appears, the cable is the usual cause. A charge-only cable
powers the board, which boots and joins Wi-Fi, but carries no data: macOS lists
no Espressif device, and the sensor's `/status` reports `"usb_connected":
false`. Use a data cable directly into the computer. When the monitor is not
wanted, `flush.sh --no-monitor` flashes and returns, and the log remains
readable at `http://<board-ip>/logs`. Treat these scripts as the canonical workflow for
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
| `OPP_SHT45_SDA_GPIO` | 3 | I2C data pin shared by SHT45 and INA219 (XIAO D1) |
| `OPP_SHT45_SCL_GPIO` | 5 | I2C clock pin shared by SHT45 and INA219 (XIAO D3) |
| `OPP_SHT45_SAMPLE_INTERVAL_SECONDS` | 5 | Awake development sampling interval |
| `OPP_SENSOR_MODBUS_ADDRESS` | 1 | Probe RTU slave address |
| `OPP_DEVELOPMENT_ADVERTISEMENT_INTERVAL_MS` | 50 | Advertising interval while always awake; production keeps the 250 ms interval |
| `OPP_REPORT_QUEUE_CAPACITY` | 32 | Reports kept until the hub acknowledges them |
| `OPP_BATTERY_MONITOR_ENABLED` | enabled | Read pack voltage and current from the INA219 |
| `OPP_INA219_ADDRESS` | 64 (`0x40`) | INA219 I2C address |
| `OPP_INA219_SHUNT_MILLIOHMS` | 100 | Shunt resistance; read the module's marking (`R100` is 100) |
| `OPP_INA219_POSITIVE_IS_CHARGING` | enabled | Matches VIN+ on the system node, VIN- on the pack |
| `OPP_BATTERY_CAPACITY_MAH` | 5000 | Combined rated capacity of the parallel cells |
| `OPP_BATTERY_SAMPLE_INTERVAL_MS` | 1000 | Battery sampling interval |
| `OPP_ALWAYS_REACHABLE` | enabled | Development only: keep advertising between reports on battery so updates and settings land at once |
| `OPP_SOIL_PROBE_ENABLED` | enabled | Poll the RS485 NPKPHCTH-S soil probe |
| `OPP_SOIL_PROBE_TX_GPIO` | 6 | RS485 board UART TX (XIAO D4) |
| `OPP_SOIL_PROBE_RX_GPIO` | 7 | RS485 board UART RX (XIAO D5) |
| `OPP_SOIL_PROBE_DE_GPIO` | 4 | RS485 driver enable, driven by the UART in half-duplex mode (XIAO D2) |
| `OPP_SOIL_PROBE_POWER_GPIO` | 10 | Probe power switch (AO3400 gate, XIAO D10); -1 if always powered |
| `OPP_SOIL_PROBE_WARMUP_MS` | 2000 | Wait after power-on before the first read |
| `OPP_SOIL_PROBE_BAUD_RATE` | 4800 | Probe factory baud rate, 8N1 |
| `OPP_SOIL_PROBE_SAMPLE_INTERVAL_SECONDS` | 10 | Awake development soil sampling interval |
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

The console has Overview and Maintenance pages. Overview presents the Hub
configuration, the ESP32-C3, and the SHT45, soil-probe and battery sections,
with live ESP-IDF logs at the bottom. Each switchable sensor's section header
carries its Enabled switch beside the state label. Maintenance persists the website color mode, accent, density, IANA timezone, `/status`
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
| `GET` | `/api/v1/soil/registers?function=4&start=0&count=16` | Debug: read raw soil-probe registers (function 3 or 4, up to 32) |

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
`/status`, and displayed in the Overview's SHT45 section, whose header shows the
status badge and the persisted enable switch. The Overview derives
absolute humidity in g/m³ from each temperature and relative-humidity sample using
the Magnus saturation-vapor-pressure approximation.
The section stays visible, showing `Unavailable` and `--`, when the SHT45 has no
valid reading. A failed
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
report ID through queued, reporting, acknowledged, unacknowledged, and failed
states. A forced report is an ordinary report: on a sensor that belongs to a hub
it joins the delivery queue and shows acknowledged when the hub's
acknowledgement names it, however long that takes. A sensor no hub owns cannot
be acknowledged, so its forced report ends unacknowledged after its window. A
full queue fails the request rather than dropping an earlier report.

The service currently has no encryption, authentication, or physical-presence
gate. It is suitable only for prototype validation on a trusted bench.

## Production deep-sleep lifecycle

Enable `OPP_PRODUCTION_LIFECYCLE` to replace the powered diagnostics runtime with
one bounded wake cycle. Production firmware reads the SHT45 once, omits the complete air
object group if acquisition fails, advertises a fresh contract-v3 report with a
new report ID and eFuse-derived `sensor-<DEVICE_ID>` name, explicitly
stops/deinitializes NimBLE, and enters timer deep sleep. The advertisement window
defaults to 3000 ms at a 250 ms interval; the sleep interval defaults to 30 minutes
until hub configuration is applied.

Production firmware also reads the soil probe once per wake and reports
whichever of air and soil succeeded. It switches the probe on through GPIO10,
waits the warm-up, reads once, and switches it off again on every path, so the
probe is unpowered during deep sleep. The production soil path is built but
has not been flashed or measured, and a 2 s warm-up is too short for pH. See
[sensor power management](power-management.md) for the power-domain model,
RTC timer behavior, peripheral shutdown requirements, and battery-life method.

## Host tests

The protocol modules do not require ESP-IDF. From the repository root:

```sh
sh scripts/test-sensor.sh
```

The script builds with C11 and treats all warnings as errors. Tests cover the
known Modbus request CRC, valid and corrupted responses, signed/scaled values,
the NPK-type probe's request and reply exactly as printed in its manual (CRCs
included), out-of-range probe values, both register-reading functions
(`0x03`/`0x04`),
the contract-v3 main, supplementary and beacon packets byte for byte against
`protocol/fixtures/bthome-v3.json`, partial source omission, rounding and range
limits, the acknowledgement token, the delivery queue's order, capacity,
acknowledgement rules and restart behaviour against an in-memory store, and
stable local-name formatting.

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

Firmware 0.13.3 reads the soil probe in the always-awake diagnostics mode. It
was flashed by USB and, with the battery fitted and the needles in water,
reported moisture, temperature, conductivity, pH and N/P/K; the SHT45 read
cleanly on D1/D3 alongside it. The soil values reach the hub over BTHome.
In clean tap water it settled at moisture 100 %, 23.8 C, 41 uS/cm and pH 6.8.
Not yet validated: 100 consecutive probe reads without a retry, soil readings
in real soil against a reference, pH against a buffer, and the production path.

### Soil probe

The firmware reads a ComWinTop NPKPHCTH-S (the five-needle "NPK type" probe)
with one Modbus RTU function `0x03` request for registers `0x0000`-`0x0006`:
moisture (0.1 %), temperature (0.1 C, signed), conductivity (uS/cm), pH (0.1),
and nitrogen, phosphorus and potassium (mg/kg). The register map and the host
test vector come from the CWT "NPK type" manual V1.4. Frames with a bad address,
length or CRC, or with moisture, temperature or conductivity outside the probe's
measuring range, are rejected.

All seven values reach the hub: moisture, soil temperature and conductivity in
the contract-v3 main packet, and pH and N/P/K in the supplementary packet (see
[BTHome reports](#bthome-reports)). A failed read logs the raw reply bytes,
which distinguish a swapped A/B pair, a wrong baud rate and a wrong address.

Each sample is attempted up to three times, 100 ms apart, because replies on
the prototype occasionally arrive short or with flipped bits. A sample fails
only when all three attempts do; a failed sample clears the previous one rather
than reporting it as current. The console's Overview keeps both the SHT45 and
soil sections on screen at all times and shows `Unavailable` and `--` with the
last error when a sensor has no current reading. Each sensor section's header has its
Enabled switch beside the state label; the soil-probe switch works like the
SHT45 one: it is saved with the UI configuration, and
switching it off stops polling at once, clears the reading, leaves soil out
of reports, and drives GPIO10 low to cut the probe's power (saving 28.5 mA).
Switching it on powers the probe, waits `OPP_SOIL_PROBE_WARMUP_MS`, and reads
again; the log records how long the probe took to answer. `/status` reports
`soil_probe_powered`. The production cycle powers the probe only for its one
read and cuts power on every path. The first pH reading after power-on is
unreliable (see [the hardware notes](../sensor/hardware/README.md#probe-power-switch)).

The debug route `GET /api/v1/soil/registers` reads any register range with
function `0x03` or `0x04` and returns the raw values, so a probe's map can be
checked against the probe itself instead of its manual. It shares the bus with
the sampling task under one lock. A probe that answers with a Modbus exception
yields `422`, no reply `504`, and a garbled reply `502`.

| Symptom | Likely cause |
| --- | --- |
| Moisture, temperature and pH exactly 0 while conductivity reads | Probe supply too weak: the node is on USB without its battery |
| Every read times out | A/B swapped, wrong baud rate or address, or no probe power |
| Frames a byte short, or last bits flipped | Line or supply noise; retries recover them. Check the battery and try the 120R switch off |
| All values 0 in air, including temperature | Needles in air explain moisture and EC of 0 (pH in air is noise, not 0); a 0.0 temperature means the supply problem above |
| pH or EC unexpected in water | Check the container first: residue in one glass read pH 4.5 and 97 uS/cm where clean tap water read 6.8 and 41. pH also takes minutes to settle |

### Battery monitor

The INA219 sits between the pack and the system node, so it measures the net
current into or out of the cells from every source and load: the IP2312, the
XIAO's own charger, the controller, and the MT3608 and probe. The monitor reads
it every second on the shared I2C bus (D1/D3, with the SHT45), continuously
converting with 128-sample averaging so that radio bursts are smoothed.

- **Current** is the shunt voltage divided by the shunt resistance, so no
  calibration register is involved. Positive is charging. Below 15 mA either
  way the pack is `idle`.
- **Charge level** starts from the pack voltage on a resting Li-ion curve,
  then counts charge in and out. The count is pinned to 100 % when a charge
  ends: at least 4.18 V with the current below C/20, held for a minute. The
  hold matters because the XIAO's own charger delivers only about 250 mA,
  below C/20 for its whole charge. From then on the level is marked
  calibrated. Until then it is an estimate: voltage reads low under
  load and high while charging. The count survives resets but not removing
  the battery.
- **Time left** divides the remaining charge by a five-minute average current,
  to empty while discharging or to full while charging; the Overview labels it
  `Until empty` or `Until full`. The average restarts when the current reverses,
  so unplugging a charger does not leave minutes "until full" on a pack that is
  now discharging. Time to full is
  optimistic, because the current tapers near the end.

With a 0.1 ohm shunt the resolution is 0.1 mA and the range about 3.2 A; an
overflow is flagged. The shunt value and capacity are build settings and must
match the parts. `/status` exposes the values as `battery_*` fields, and the
Overview shows them in a Battery section.

### BTHome reports

Firmware sends [contract v3](../protocol/README.md#bthome-contract-version-3).
Each report takes the next report ID, gathers the latest SHT45 sample, soil
sample (with pH and N/P/K), battery level and voltage, and the acquisition time
when the clock is trusted, and becomes two packets: a main packet with the core
measurements (24 bytes at most) and a supplementary one with battery, pH and
N/P/K, sent even when it carries only the report ID. The broadcaster swaps
between the two every 500 ms within one advertising window, since legacy
advertising accepts new data while it runs. With no SHT45 or soil sample the
sensor sends a beacon, the single byte `0x40`.

### Durable delivery and freshness

A sensor that belongs to a hub keeps every report until the hub has stored it
(see [durable delivery](../protocol/README.md#durable-delivery-and-acknowledgement)),
and shows the hub its newest data within a second or two. Firmware 0.18.0:

- **Schedule.** Report n is due at the start plus n intervals, as set by the
  hub; the interval is re-read on every pass. An advertising window never runs
  past a due report: reachability windows end at the next deadline, and a
  report's own window ends half a second before the next one. More than an
  interval behind (a long hub connection) restarts the schedule rather than
  bursting to catch up.
- **Queue.** Each report is written to a queue in NVS (namespace `rqueue`: a
  `counters` blob with head and tail, one blob per slot) before it is first
  advertised. `OPP_REPORT_QUEUE_CAPACITY` (default 32) bounds it; a full queue
  makes no new reports and never overwrites one. A sensor no hub owns does not
  queue, and a release by the hub clears it.
- **Freshness.** The sensor advertises its newest report, so what the hub
  receives over the air is current whatever is still queued. With the queue
  empty it sends beacons.
- **Bulk drain.** The hub drains the queue over one connection in pages of up
  to eight reports, acknowledging each page cumulatively; only reports already
  put on a page during that connection can be acknowledged, and removal is saved
  before the sensor answers. Reads return pages from the drain request to the
  end token or the disconnect.

The console's Hub section shows "Waiting for hub" and `/status` reports
`report_queue_depth` and `report_queue_capacity`.

### Development reachability

An owned sensor on battery normally advertises only at its reports (every 30
minutes by default), while USB data is attached, and for two minutes after a
hub connection. The hub can reach a sensor only while it advertises, so a
settings change or update sent between reports waits for the next one; this
stalled an install for more than six minutes on 2026-09-26.
`OPP_ALWAYS_REACHABLE` (development builds, on by default) keeps the sensor
advertising continuously instead. It is not deep sleep: the development
lifecycle never sleeps, and deep sleep exists only in the production lifecycle.
