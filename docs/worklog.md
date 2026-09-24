# Open Plant Pulse worklog

## 2026-09-12

### Project context and architecture

- Reviewed the repository structure and documented the intended sensor-to-hub architecture.
- Confirmed the planned sensor lifecycle: wake, sample sensors, advertise BTHome data, and return to deep sleep.
- Kept continuous Wi-Fi and the device web console scoped to powered development and diagnostics because they conflict with the production low-power lifecycle.
- Confirmed the current hardware target is a Seeed Studio XIAO ESP32-C3 revision 0.4 with 4 MB physical flash. The current firmware image and partition configuration still use a 2 MB flash header.

### SHT45 and sensor planning

- Added `docs/sht45-first-node.md` with wiring, command, CRC, integration, and bring-up guidance.
- Selected GPIO6 for SDA and GPIO7 for SCL as candidate SHT45 pins, pending assembled-hardware verification.
- Preserved the distinction between SHT45 ambient temperature and the ESP32-C3 internal die temperature.
- Left RS-485 soil-probe acquisition unimplemented until hardware behavior and register details are validated.

### SHT45 firmware support

- Added a dedicated ESP-IDF SHT45 component using I2C port 0, address `0x44`,
  100 kHz, and configurable GPIO6 SDA/GPIO7 SCL defaults.
- Added the high-precision no-heater `0xFD` measurement, bounded 100 ms bus
  transactions, CRC-8 validation for both words, conversion, and humidity clamping.
- Added a five-second development monitor that invalidates stale data on failures
  and retries an absent sensor without blocking the web console.
- Exposed SHT45 availability, ambient temperature, humidity, and sample age in
  `/status`, added live values to the Sensors view, and added an Overview section
  that appears automatically while the sensor has a valid reading.
- Added host tests for the documented CRC fixture, decoded values, corrupted CRC,
  and 0-100% humidity clamping.

### Mechanical enclosure

- Repaired the Blender MCP environment and established a working Blender automation path.
- Created the parametric enclosure source at `sensor/hardware/enclosure/source/air-node-case-v1.py`.
- Designed an FDM-oriented enclosure for the XIAO ESP32-C3, SHT45, RS-485 carrier, external BLE antenna, dual-18650 holder, sliding lid, USB-C access, sensor exposure, and soil-probe cable routing.
- Generated enclosure exports and iterated toward an approximately 83.8 x 83.3 x 27.0 mm assembly.

### ESP-IDF toolchain and board bring-up

- Installed and validated ESP-IDF v5.5.5 at `~/esp/esp-idf` for the ESP32-C3 target.
- Added canonical firmware tooling:
  - `sensor/firmware/idf-env.sh`
  - `sensor/firmware/build.sh`
  - `sensor/firmware/flush.sh`
- Built and flashed the physical XIAO ESP32-C3 over `/dev/cu.usbmodem14801`.
- Confirmed the Wi-Fi station MAC is `1c:db:d4:f0:ba:e4` and verified its reserved address at `192.168.0.111`.
- Kept local Wi-Fi credentials in ignored `sensor/sdkconfig`; no credential values were added to source or documentation.

### Device web console

- Implemented a native ESP-IDF `esp_http_server` development console.
- Refactored the original diagnostics implementation into the generic `web_ui` module.
- Added station-mode Wi-Fi with DHCP and automatic reconnect.
- Added a bounded 16 KiB in-memory log ring while preserving USB serial logging through `esp_log_set_vprintf()`.
- Added embedded, dependency-free frontend assets:
  - `sensor/firmware/main/web/index.html`
  - `sensor/firmware/main/web/app.css`
  - `sensor/firmware/main/web/app.js`
- Added responsive Overview, Sensors, Logs, and Maintenance views.
- Added `/logs`, `/status`, and a no-content `/favicon.ico` route.
- Added `docs/proposals/sensor-web-ui.md` with the staged web-console architecture and delivery plan.

### Chip temperature

- Integrated the ESP32-C3 internal temperature sensor using `esp_driver_tsens`.
- Added `chip_temperature_c` to `/status`, returning `null` when unavailable.
- Added a Chip temperature card to Overview labeled `Internal die, not ambient`.
- Initialized the temperature peripheral before starting Wi-Fi so `/status` cannot race an uninitialized sensor handle.
- Verified a live reading around 51.9-53.9 C while Wi-Fi was active. This is a device-health measurement, not room temperature.

### Persistent UI configuration

- Added a versioned UI configuration module:
  - `sensor/firmware/main/web_ui_config.c`
  - `sensor/firmware/main/web_ui_config.h`
- Stored the bounded JSON configuration in the dedicated `web_ui` NVS namespace so it survives resets, power loss, and normal firmware flashing.
- Added configuration endpoints:
  - `GET /api/v1/config/ui`
  - `PUT /api/v1/config/ui`
  - `POST /api/v1/config/ui/reset`
- Added strict validation for schema version, known fields, theme values, card IDs, duplicate cards, card spans, body size, and the requirement that at least one card remain visible.
- Added customization controls for:
  - Dark, light, or system color mode.
  - Green, blue, or amber accent.
  - Comfortable or compact density.
  - Overview card order, visibility, and one- or two-column width.
  - JSON import, export, and reset to defaults.
- Added immediate browser previews with debounced automatic persistence after each control change.
- Documented storage behavior and the API in `docs/firmware.md`.

### Defects found and corrected

- Corrected embedded asset linker symbols after ESP-IDF stripped the `web/` path from generated symbol names.
- Added missing component dependencies discovered during builds, including `esp_timer` and `esp_driver_tsens`.
- Fixed a lifetime bug where parsed cJSON string pointers outlived their JSON document by copying values into owned fixed-size fields.
- Fixed an HTTP worker stack overflow during configuration writes by moving bounded JSON buffers to the heap and increasing the server task stack to 6 KiB.
- Adjusted temperature initialization ordering to remove a potential early-request race.
- Avoided using `path` as a zsh variable because it aliases the shell's `PATH` array.

### Validation completed

- Built the final firmware successfully with `sensor/firmware/build.sh`.
- Flashed the physical board successfully with `sensor/firmware/flush.sh --no-monitor`.
- Final firmware size was approximately `0xe12a0` bytes, leaving about 12% of the current 1 MiB application partition free.
- Verified live `/status` responses, including connection state, uptime, free heap, MAC, and chip temperature.
- Verified default configuration reads, valid writes, strict `400` rejection of malformed/unknown configuration, and reset behavior.
- Verified custom configuration survived reflashing without NVS erase.
- Exercised the actual Maintenance controls to save and reset a custom layout.
- Verified desktop rendering at 1440 x 900 and mobile rendering at 390 x 844 without horizontal overflow.
- Left the physical device running the default dark, green, comfortable layout at `http://192.168.0.111/`.

### Device restart

- Added `POST /api/v1/restart` with a bounded, strictly validated
  `{"confirm":"restart"}` JSON request.
- Scheduled restart through a 750 ms one-shot timer so the HTTP client receives
  `202 Accepted` before `esp_restart()` runs.
- Added a confirmed Maintenance action with disabled, restarting, reconnecting,
  success, and failure states.
- Verified invalid and duplicate requests are rejected, the physical device
  restarts and reconnects, and its NVS-backed UI configuration is preserved.

### Firmware versioning

- Established `sensor/version.txt` as the authoritative Semantic Versioning
  source, beginning with firmware `0.1.0`.
- Exposed the compiled ESP-IDF application version as `firmware_version` in
  `/status` and included it in the startup log.
- Added the live firmware version to the web console browser title and header
  subtitle, including on narrow screens.

### RTC clock synchronization

- Added plausible-date validation for 2024 through 2099 and bounded SNTP startup
  after DHCP when the clock is invalid or due for drift correction.
- Added configurable 15-second attempts, 15-minute failure retries, 12-hour
  correction cadence, and clock-jump logging without rewriting prior samples.
- Added retained SHT45 sample sequence numbers, monotonic offsets, and nullable
  acquisition-time UTC values so unsynchronized samples remain orderable.
- Added UTC date/time and RTC status cards to the Overview ESP32-C3 section, with
  automatic migration of existing dashboard schema-v1 layouts.

### Inferred power source

- Added USB Serial/JTAG host detection to `/status` as `usb_connected` and
  `power_source`.
- Added a configurable Overview Power card showing `USB` or `Battery (inferred)`,
  with automatic migration of existing schema-v5 dashboard layouts.
- Documented that USB wins when both sources are present and that the battery state
  assumes USB chargers and power banks are not used.
- Built and flashed firmware 0.1.5, then validated the USB state and schema-v5 to
  schema-v6 migration on the connected ESP32-C3.

### Configurable status polling

- Added a Maintenance setting for `/status` refresh intervals from one second to
  one hour, including Off while retaining the initial page-load request.
- Persisted the interval in the NVS-backed UI configuration and migrated existing
  schema-v6 configurations to the prior two-second default.
- Bumped firmware to 0.1.6, passed the repository checks and ESP-IDF build, flashed
  the connected ESP32-C3, and verified 0.1.6 in its serial startup log.
- Verified through the live device APIs that the retained configuration migrated
  to schema v7 with the two-second interval and seven-card layout.

### Absolute humidity dashboard metric

- Added browser-side absolute humidity derivation from SHT45 temperature and
  relative humidity readings using the Magnus approximation.
- Added the AH value to the Overview ambient-sensor grid and documented that it
  is derived rather than supplied by the sensor protocol.
- Bumped firmware to 0.1.7, passed the repository checks and ESP-IDF build,
  flashed the connected ESP32-C3, and verified 0.1.7 and the AH card through the
  live device APIs.

### Remaining work

- Validate every status-polling choice and Off behavior in a browser, including
  continued clock rendering while automatic `/status` polling is disabled.
- Validate SHT45 readings and failure recovery on the connected physical board.
- Validate the inferred battery transition while the node remains battery-powered
  after its USB data connection is removed.
- Validate the purchased RS-485 soil probe register map and implement bounded acquisition.
- Add battery voltage, charge state, Wi-Fi signal, reset reason, and stale-reading health indicators.
- Complete versioned status/log APIs and log controls described in the web UI proposal.
- Design authentication or a physical-presence policy before exposing sensitive maintenance operations.
- Revisit the flash header and partition table before adding OTA or larger persistent assets.
- Return the production firmware to a BLE/deep-sleep lifecycle after bench diagnostics are complete.

## 2026-09-13

### Hub BTHome phases 1–2 validation

- Installed the declared hub development dependencies in an isolated `.venv` and
  confirmed the dependency set is consistent with `pip check`.
- Exercised CoreBluetooth discovery on macOS and observed 40 BLE devices without a
  permission or adapter error.
- Started the production `BleakSubscriber` with its BTHome `0xFCD2` filter and
  observed `scanner.status` remain `scanning` with no error.
- Observed no BTHome broadcaster, so `last_receive_at` remained null; physical
  BTHome reception, multi-sensor radio behavior, and Linux BlueZ permissions remain
  unvalidated.
- Completed phases 1–2 using captured evidence: repeated replay across restart and
  the production scanner callback path keep three sensor identities isolated,
  suppress duplicate durable samples, preserve partial-source nulls, recover from
  adapter interruption, bound receive diagnostics, and report scanner/database
  health separately.

### Hub phase 3 and sensor phase 4 start

- Added schema-v3 sensor management fields and browser workflows for the sensor
  inbox, three-sensor fleet monitoring, enrollment, names, rooms, profiles,
  thresholds, archive/restore, and replacement with optional history merge.
- Added sensor-scoped latest-reading, history, and care-log queries, responsive
  navigation/deep links, freshness and unavailable-source states, and same-origin
  checks for browser mutations.
- Bumped sensor firmware to 0.2.0 and added an opt-in air-only production lifecycle:
  fresh SHT45 acquisition, stable BTHome v2 identity, retained packet ID, bounded
  non-connectable advertising, explicit NimBLE shutdown, and timer deep sleep.
- Host sensor tests and 46 hub tests pass. The ESP-IDF 5.5.5 production-mode build
  succeeds. An intermediate 0.2.0 image flashed successfully, after which the USB
  serial device disappeared on sleep before runtime logs could be captured; the
  final radio-shutdown adjustment therefore still needs reflashing. Running-version,
  physical BLE delivery, repeated wake cycles, power measurements, soil-probe
  cleanup, and the 24-hour soak remain unverified.
- Bumped firmware to 0.2.1 and disabled `OPP_PRODUCTION_LIFECYCLE` in the local
  ignored `sensor/sdkconfig` to prepare an always-awake recovery image. Fixed the
  resulting development-build unused-variable warning; canonical checks and the
  ESP-IDF 5.5.5 build pass. The image contains version 0.2.1 and uses 0xf68a0 bytes
  of the 0x100000-byte app partition, leaving 4% free.
- Initial `sensor/firmware/flush.sh --no-monitor` attempts were blocked while no
  ESP32 serial port was present. After USB recovery exposed
  `/dev/cu.usbmodem14801`, the canonical script flashed 0.2.1 successfully and
  verified every written image hash. Releasing BOOT and resetting produced normal
  flash boot; serial logs confirmed running version 0.2.1 and repeated SHT45 samples.
  The HTTP `/status` response confirmed USB power, Wi-Fi at `192.168.0.111`, clock
  synchronization, and a current SHT45 sample. Physical BLE delivery, production
  sleep cycles, power, and soak validation remain unverified.

### Persistent hub-to-sensor configuration

- Bumped firmware to 0.3.0 and added a versioned connected-BLE configuration
  characteristic for plant name, room, and 5-minute-to-24-hour reporting interval.
- Firmware validates and stores accepted configuration in NVS, renders it on the
  sensor dashboard, and applies the interval to future awake reports or deep sleep.
- The hub tracks desired and acknowledged revisions, retries failures after later
  reports, and shows pending/applied/retrying state on sensor detail pages.
- All 60 canonical host checks pass and the ESP-IDF 5.5.5 firmware build succeeds.
  Initial validation was blocked while no serial device was available.

## 2026-09-14

### Firmware 0.3.0 physical configuration validation

- Flashed the always-awake firmware 0.3.0 image through the canonical
  `sensor/firmware/flush.sh` workflow; esptool verified every written image hash.
  Serial boot output confirmed version 0.3.0 and restored NVS revision 1 with plant
  `white bird`, room `living`, and a 30-minute interval.
- Diagnosed `/status` connection resets as an HTTP-task stack-protection fault in the
  enlarged `snprintf` response. Moved the 2,432-byte response buffer to the heap with
  allocation and truncation checks, reran all 60 host checks, rebuilt, and reflashed.
  Five consecutive `/status` requests then returned HTTP 200 without a reset.
- The live `/status` payload reported version 0.3.0, revision 1, `white bird`,
  `living`, 30 minutes, and USB power. Headless Chrome rendered the same revision,
  plant, room, and interval in the board's Overview page.
- Direct CoreBluetooth GATT access from the project `.venv` wrote and read back the
  revision-1 payload exactly. After the final flash/reset, an idempotent read-back
  completed at 08:13:25 UTC, 1,801.8 seconds after the estimated 07:43:24 UTC boot,
  physically confirming the first configured 30-minute report window.
- The direct GATT client consumed that advertising connection, so the concurrently
  running hub did not store a second cadence sample. Multi-sensor physical behavior,
  production deep-sleep operation, measured current/energy, and the 24-hour soak
  remain unverified. `/status` only confirms that the current source is USB; no power
  meter was connected.

## 2026-09-15

### Firmware 0.5.0 manual report acknowledgement

- Added an always-awake diagnostics **Report now** control that interrupts the wait
  for the next configured interval without moving that scheduled deadline.
- Forced reports carry a standard BTHome button-press event and expose a versioned
  request-ID/packet-ID token through the connected-BLE service. The Hub writes that
  exact token back only after durable ingestion and suppresses duplicate GATT attempts
  from repeated callbacks in one advertising burst.
- The sensor dashboard tracks queued, reporting, acknowledged, unacknowledged, and
  failed states and shows both the request and packet IDs. Scheduled and production
  deep-sleep reports remain one-way and do not wait for a Hub.
- Hardware testing at the persisted 3-second interval exposed and fixed starvation
  when the advertising window occupied the complete interval. The scheduler now
  prioritizes a queued manual request as soon as the active window closes without
  moving the periodic deadline.
- The first physical report was durably accepted as packet 18, but macOS retained
  the old GATT service layout and could not discover a newly added characteristic.
  The six-byte token was moved onto the established configuration characteristic,
  with version and payload length distinguishing the two protocols; this avoids a
  service-cache migration requirement.
- All 65 repository checks and JavaScript syntax validation passed. ESP-IDF 5.5.5
  built firmware 0.5.0 with 17% of the app partition free. The canonical flash
  workflow wrote the final image to `/dev/cu.usbmodem14801` and verified every hash.
- Live `/status` confirmed firmware 0.5.0, revision 5, and the persisted 3-second
  interval. Physical request 1 emitted packet 6; the Hub database accepted it at
  `2026-09-15T02:10:44.026289Z`, and firmware recorded the matching acknowledgment
  before report completion. Headless Chrome rendered `Request 1 · packet 6 · report
  stored and Hub acknowledged` from the board's Maintenance page.

## 2026-09-23

### Repository history and hub Wi-Fi onboarding design

- Committed the previously uncommitted body of work in four logical commits:
  the BTHome contract-v2 and connected-BLE protocol definitions, sensor firmware
  0.2.0 through 0.5.1, hub BLE ingestion with sensor management, and the matching
  documentation. All 67 canonical host checks passed before committing.
- Reviewed the existing Wi-Fi path from a customer's perspective. Credentials are
  compile-time `OPP_WIFI_SSID`/`OPP_WIFI_PASSWORD` constants baked into the image;
  no provisioning mechanism of any kind exists, and none is needed on the sensor
  because production telemetry is BLE-only.
- Recorded review findings: changing networks requires a rebuild and reflash, the
  password sits in unencrypted flash, the disconnect handler reconnects immediately
  without backoff and never logs the reason code, the auth threshold silently
  rejects open networks, and no customer-facing setup document exists.
- Added `docs/proposals/sensor-onboarding.md` covering both out-of-box goals for a
  new sensor: registering with the hub and receiving Wi-Fi credentials. The hub is
  the onboarding interface and the sensor raises no access point, because the
  connectable BLE path already exists, needs no router, and costs no flash.
- Recorded the prerequisite found while designing it: the configuration
  characteristic is declared `F_READ | F_WRITE` with no `ble_hs_cfg.sm_*` settings
  anywhere, so the link has no pairing, bonding, or encryption and cannot carry a
  Wi-Fi password until it does.
- Settled the design on two states derived from bond presence, an LE Secure
  Connections handshake whose LTK is derived rather than transmitted, and a
  six-digit factory passkey that provides both man-in-the-middle protection and
  proof of physical possession. The passkey replaces an earlier button-based
  ownership check; no GPIO button is read in firmware, and the BTHome button event
  is produced only by a web-console forced report that a factory-fresh sensor
  cannot reach.
- Recorded the build settings the design depends on: `CONFIG_BT_NIMBLE_NVS_PERSIST`
  is not set, so bonds would live in RAM and be lost on every deep-sleep wake, and
  `CONFIG_BT_NIMBLE_MAX_BONDS` must fall from 3 to 1 for the one-hub rule.
- Recorded that link encryption covers connections only. BTHome advertisements stay
  unencrypted under contract v2, so telemetry privacy needs a separate 16-byte bind
  key; the credential payload must leave room for one to avoid a second migration.
- Reordered the delivery plan to be interface-first: a dedicated hub management
  page and a separate guided onboarding flow, both driven by a simulated sensor
  with no firmware or hardware, produce the state and field contract that the
  later firmware phases implement.
- Recorded a platform constraint found while planning that work: `bleak` cannot
  initiate pairing on macOS, where CoreBluetooth raises `NotImplementedError` and
  the operating system presents its own passkey dialog. The onboarding flow cannot
  own a passkey field there, and macOS and Linux need separate validation.
- Moved Wi-Fi credentials to hub level: one household network entered once on the
  hub settings page, kept in the operating system keychain rather than the readings
  database, with each sensor carrying only an enable flag. Turning that flag off
  stops the sensor's HTTP server and leaves the network, which makes it the control
  for the product's only unauthenticated network surface; it defaults to off. This
  makes Wi-Fi a runtime rather than compile-time decision, and disables SNTP
  synchronisation along with it.
- Measured the advertisement budget for encrypted telemetry against the current
  encoder. Encryption adds 8 bytes into a 24-byte allowance: today's air-only
  payload fits at 17 bytes, but air plus the soil probe reaches 26. Dropping the
  packet-id object when encrypted frees exactly the 2 bytes needed, since the
  encryption counter already provides deduplication and replay protection.
- Added a Check network action to the household network settings. It reports range,
  band, and security from the hub's own scan, and deliberately does not test the
  password, because the hub joining to try it would disconnect the hub and only a
  sensor joining proves a password. It catches the mistyped name and the 5 GHz-only
  network, needs Location permission on macOS, and must degrade to saying it could
  not look on a hub with no Wi-Fi adapter.
- Established how the Wi-Fi password is verified. It cannot be checked offline,
  because only the four-way handshake proves a WPA2 passphrase, and the hub cannot
  perform it without dropping its own connection; the development hub is on Ethernet
  with its Wi-Fi interface unassociated, so it has no route to try at all. The test
  therefore runs through an enrolled sensor using the forced-report path, returning
  a classified result in seconds. Where the operating system already holds the
  password, the settings page offers to fill it in from the keychain or from
  NetworkManager, so it need not be retyped.
- Made encrypted telemetry the default rather than a setting, which makes contract
  v3 the contract for onboarded sensors and removes the keys page from the
  interface. Each sensor is given its own 16-byte key during onboarding. The hub
  must therefore be able to reveal that key for Home Assistant, must store it
  durably rather than in the keychain because every advertisement needs it, and the
  sensor's counter must stay monotonic across deep sleep or replay protection
  rejects it after every wake.
- Removed the keys and pairing page. Its contents either duplicated the sensors tab,
  belonged in the onboarding flow, or were per-sensor data; the Home Assistant key
  now sits on the sensor it belongs to.
- Required the first reading to arrive during onboarding rather than at the next
  interval, targeting 30 seconds. The hub requests a forced report over the link it
  is already connected on, reusing the existing `opp_force_report_request()` path,
  which is wired only to the web console today. Recorded the failure case seen on
  the bench: a forced report returns `no valid sensor sample` when the SHT45 is not
  responding, so the flow must report that rather than wait.
- Rejected BLE 5 extended advertising as the way out. BTHome records it as
  infeasible because `bleak` lacks support, and `bleak` is the library both this hub
  and Home Assistant depend on, so the sensor would transmit correctly and no
  receiver would hear it.
- Chose power-cycle counting as the physical reset, filtered by `esp_reset_reason()`
  so deep-sleep wakes do not advance it. The RESET button cannot erase anything by
  itself, and GPIO9 is a strapping pin the enclosure does not expose.
