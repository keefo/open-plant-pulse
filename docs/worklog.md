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
- Settled that there is no Wi-Fi test action. A WPA2 passphrase cannot be checked
  offline, only the four-way handshake proves it, and the hub cannot perform one
  without dropping its own connection; the development hub is on Ethernet with its
  Wi-Fi interface unassociated, so it has no route to try at all. Saving already is
  the test: every sensor whose flag is on tries to join and reports a classified
  result in seconds through the forced-report path. Where the operating system holds
  the password, the settings page offers to fill it in from the keychain or from
  NetworkManager, so it need not be retyped.
- Renamed the primary navigation entry from Fleet to Plants.
- Resequenced the delivery plan around the hardware rather than around features.
  Phases 1 to 5 are written, built, and host-tested with no board and no person;
  phase 6 groups everything that cannot be exercised otherwise, because the macOS
  pairing dialog takes a human, a flash may need the BOOT button, and the
  power-cycle reset ignores `ESP_RST_SW` by design so a software restart cannot
  stand in for it. Nothing in phases 2 to 5 may be called working until phase 6
  says so. The bench sensor's unresponsive SHT45 is a precondition for that session,
  since the first-reading target cannot be measured without a valid sample.
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

## 2026-09-24

### Onboarding phase 1: the hub interface

- Added schema 11: `onboarding_state`, `wifi_enabled`, `wifi_state`, `wifi_failure`,
  and `wifi_address` per sensor, plus one household `wifi_ssid` on the hub. The
  network name is stored without a password, since the database is backed up and
  exported.
- Added store methods for the household network, the per-sensor console switch, the
  reported join result, and the onboarding state. Forgetting the network switches
  every console off; returning a sensor to onboarding clears everything derived from
  its bond. The five join failure reasons are enumerated and enforced, so the browser
  can never be handed a failure it cannot explain.
- Added a Settings page separate from the dashboard, with Sensors and Wi-Fi tabs, and
  a four-step add-a-sensor flow, in its own script. Extended the router from two
  hard-coded pages to a list, and renamed the fleet navigation entry to Plants.
- Added a throwaway preview server so the journey can be walked by a person.
- All 99 canonical host checks pass. Verified in headless Chrome that the settings
  page renders a paired sensor with its address, the Wi-Fi tab loads the saved
  network and hides the sensors panel, and the wizard shows step 1 of 4 with both
  unclaimed sensors listed and Continue disabled until one is chosen.
- Recorded the resulting field and failure contract in the proposal. No firmware
  exists for any of it yet; nothing here has touched hardware.

### Onboarding phase 2 and part of phase 3: firmware

- Bumped firmware to 0.6.0. Configured NimBLE LE Secure Connections with bonding,
  a passkey, and `BLE_HS_IO_DISPLAY_ONLY`, and changed the configuration
  characteristic from plain read/write to requiring an encrypted link. Added a
  six-digit passkey generated on first boot into a `factory` NVS namespace that a
  reset must not clear, refused a second bond so one sensor stays bound to one hub,
  and exposed `onboarding_state` in `/status`.
- Set `CONFIG_BT_NIMBLE_NVS_PERSIST=y` and `CONFIG_BT_NIMBLE_MAX_BONDS=1`. Without
  the first, bonds live in RAM and every deep-sleep wake would lose the hub.
- Added the Wi-Fi credential payload as connected-BLE protocol version 3 on the
  established characteristic, with an NVS store beside the device configuration.
  Host tests cover the round trip, the three payloads staying distinguishable, the
  longest permitted values fitting the budget, and malformed input being refused.
- Replaced the immediate endless Wi-Fi reconnect with a bounded backoff, and
  classified disconnect reasons into the vocabulary the hub and browser share,
  including a twenty-second watchdog for associating without receiving an address.

### Unresolved: the worktree firmware image cannot be verified

- `sensor/firmware/build.sh` exits 0 and the link map lists both `libesp_wifi.a` and
  `libmain.a(web_ui.c.obj)`, but the image it produces is 659,744 bytes where the
  bench 0.5.1 image is 1,277,776, and it contains none of the literals either
  version should carry. A byte search of the bench image finds `wifi` 56 times,
  `sht45_enabled`, and `force_report_state`; the same search of the worktree image
  finds none of them, while finding `0.6.0`, `Open Plant Pulse`, `esp_timer` and
  `nvs`.
- The image was byte-identical in size across three builds, including one after
  deleting `sensor/build` entirely. The two `sdkconfig` files differ only by the two
  intended NimBLE settings.
- The cause is not established. Treat the 0.6.0 firmware as compiling, and nothing
  more: it has not been shown to produce a correct image, it has not been flashed,
  and no pairing, credential, or Wi-Fi behaviour has run on hardware.

## 2026-09-25

### Removed the simulator, and split plant care from sensor configuration

- Deleted `simulator/`, the loopback UDP transport, the simulation fixture and the
  tests that only covered them. The dashboard tests now build their reading in
  place rather than decoding a fixture, so they no longer depend on a transport
  that does not exist. Removed the `simulated-plant-01` row and its 39,017 dummy
  readings from the running hub.
- Moved the raw report log below the configuration and made it a collapsible
  section whose state is remembered per browser.
- Gave each sensor a configuration page at `/sensors/<id>/settings`, reachable
  from the plant page and from each row in hub settings, and took the console
  switch out of that list so it exists in one place. Each page now fetches only
  what it shows.

### Why every report is recorded twice

- The receive log shows two rows per report because the sensor puts its BTHome
  service data in the advertisement and its name in the scan response, and
  CoreBluetooth always active-scans: two packets are received per advertising
  event, 0.1 to 1 ms apart, at different RSSI in 39% of cases. Nothing is stored
  twice; the second row is a diagnostics entry marked duplicate. Not changed.

### Firmware over the air

- Wrote `docs/proposals/firmware-over-the-air.md`: the hub commands over the
  bonded BLE link, the sensor downloads over the household network.
- **Hub.** Images are parsed out of the ESP-IDF application descriptor, so an
  upload that is not an `esp32c3` `open_plant_pulse` image is refused and the
  version is read rather than typed. Images are stored once per digest under
  `~/.open-plant-pulse/firmware/` and served by a separate read-only HTTP server
  on the household network (default port 8081); the management API stays on
  loopback. Settings gained a Firmware tab, and a sensor's configuration page an
  update button with progress.
- **Sensor.** `sensor/partitions.csv` replaces the single-app layout with two
  1600 KiB slots inside the 4 MB the board has always had, keeping `nvs` at its
  offset so the cable flash that introduces it costs nothing. Payload version 7
  starts an update; the sensor streams the image into the spare slot, hashes what
  it wrote, and refuses to make it bootable unless the hash matches.
- **Validated on hardware, 2026-09-25.** 0.12.0 was flashed by cable at
  1,418,944 bytes and came back with its pairing code and its place on the
  network intact. 0.12.1 was then installed **over the air in about fifteen
  seconds**, with the hub showing it downloading and then reporting the new
  version. A reset afterwards booted 0.12.1 from `ota_1`, which is only possible
  if the image had confirmed itself.
- **Rollback, also on hardware.** A deliberately broken 0.12.2 was installed over
  the air, aborted on its first boot, and the bootloader loaded the previous slot:
  `Loaded app from partition at offset 0x1b0000` followed by `Running firmware
  0.12.1 from ota_1`. That image was never committed, and both it and the pre-OTA
  0.11.2 image were deleted from the hub afterwards — installing a single-slot
  image over the air would be a one-way trip back to the cable.
- The bad case exposed a gap: the hub went on saying "restarting" for ever,
  because success is the sensor returning with the new version and nothing
  watched for it never doing so. An update still in flight after five minutes is
  now failed, naming the version the sensor is actually running. Observed
  flipping to `failed` five minutes after the rolled-back install.
- **Not validated.** A sensor that sleeps between reports, more than one sensor
  at a time, and the sensor's refusal of an image whose digest does not match:
  the hub checks its files against the name they are stored under, so it cannot
  serve a mismatching image, and proving that path needs a server built to lie.

## 2026-09-26

### Assembled node: power path and pins

- The node now runs from two parallel 18650 cells charged by an IP2312 USB-C
  module, with an INA219 in series with the pack and an MT3608 boosting the
  system node to 12.4 V for the soil probe. The full table is in
  `sensor/hardware/README.md`.
- **Pin conflict found before any Modbus code ran.** The XIAO RS485 board uses
  D4/D5 for its UART and D2 for driver enable, which is where the SHT45 was.
  The board's receiver drives D5, which would corrupt I2C, and the ESP32-C3 has
  one I2C controller, so the SHT45 moved to D1/D3 beside the INA219. The Kconfig
  defaults now describe this board: I2C on GPIO3/GPIO5, UART on GPIO6/GPIO7, DE
  on GPIO4.
- After the move the SHT45 acknowledged `0x44` but every measurement failed
  its CRC. The cause was a broken wire, not the pins; after repair it read
  cleanly. A broken I2C joint can still acknowledge an address.

### NPKPHCTH-S soil probe support (0.13.0-0.13.3)

- The purchased probe has four wires and five needles; the vendor's readme maps
  it to the "NPK type (5Pin probe)" manual V1.4, whose register map, address 1,
  4800 baud 8N1 and wire colours were used. The table's "function 0x30" is a
  typo for `0x03`; the manual's own examples use `0x03`.
- The manual's worked request and reply, CRCs included, are host test vectors,
  so the parser is checked against the vendor's document rather than itself.
- Firmware reads all seven registers every 10 s, rejects out-of-range values,
  broadcasts moisture, soil temperature and conductivity over BTHome (the hub
  already decodes them), and shows pH and N/P/K on the sensor's own page. The
  BTHome payload is at its 20-byte limit.
- **Occasional short or corrupted replies** (for example a CRC of `ef d5`
  instead of `ef 15`, and one- or two-byte replies) led to three attempts per
  sample in 0.13.2. The console's soil and SHT45 sections now stay visible and
  say `Unavailable` rather than disappearing on a failed sample.
- **Moisture, temperature and pH read exactly 0 in water** while conductivity,
  salinity and TDS were correct. A raw dump through the new debug route
  (`/api/v1/soil/registers`, 0.13.3) showed the firmware reading exactly the
  registers the manual describes, and the probe returning 0 in them. The
  cause: the node was powered from the XIAO's USB cable with no battery, so
  the only source on the system node was the XIAO's small charger, which cannot
  supply the MT3608 and probe. The probe's controller answered, but its
  moisture/temperature sensor and pH electrode did not start. With the battery
  in: moisture 77.6 %, temperature 25.7 C, pH 4.8; fully submerged, moisture
  100.0 %, temperature 25.7 C, EC 97 uS/cm, pH 4.5.
- **pH 4.5 was the container, not the probe.** The same tap water in a clean
  container read pH 6.8 (settling from 7.4), EC 41 uS/cm, 23.8 C and 100 %.
  Residue had made the water acidic and more conductive, and the probe measured
  it correctly. In air: moisture 0 %, EC 0, temperature real (unlike the
  starved probe's 0.0), pH noise.
- N/P/K follow conductivity (1/2/7 at 41 uS/cm) and are estimates, not
  measurements.

### Deployment and reachability

- 0.13.0 was installed over the air through the hub in about 25 s. A
  follow-up install stayed `pending` for more than six minutes: an owned
  sensor on battery advertises only at its reports, so the hub had no window
  to deliver it. The install was cancelled.
- Deployments were then restricted to `flush.sh` over USB-C. Later the same
  day, with development builds always reachable, over-the-air installs were
  allowed again; `AGENTS.md` and `docs/firmware.md` describe both paths. `OPP_ALWAYS_REACHABLE` keeps
  development builds advertising continuously. The development lifecycle
  never deep sleeps; deep sleep exists only in the production lifecycle.
- The first USB attempts failed because the cable was charge-only: the board
  booted and reported `usb_connected: false`, and macOS listed no Espressif
  device. A data cable fixed it.
- 0.13.2, 0.13.3 and 0.13.4 were flashed by `flush.sh`, each with all four
  regions hash-verified, and booted reporting their versions over serial and
  `/status`.
- The Maintenance page's soil-probe row still said "Not implemented" and its
  switch saved a setting nothing read. 0.13.4 wires it like the SHT45 switch;
  verified on the device: off logged "Soil probe sampling disabled" and
  cleared the reading within a second, on resumed sampling immediately.

### Battery monitor (0.14.0-0.14.1)

- The SHT45 driver used to create and delete the I2C bus itself, so switching
  it off would have removed the INA219's bus too. A shared, reference-counted
  bus now serves both on D1/D3.
- The INA219 is read every second with 128-sample averaging. Current is shunt
  voltage over shunt resistance (0.1 ohm assumed), positive into the pack.
  The charge level starts from a resting Li-ion voltage curve, counts charge in
  and out, and is pinned to 100 % at the end of a charge. It survives resets.
- First reading on the device: 4.16 V, +259 mA, "charging", with the C3 on
  USB, so the XIAO's own charger charges the pack through the INA219. This
  confirms the sign of the wiring.
- **0.14.0 declared the pack full 41 s after boot.** Its rule (4.15 V, current
  below C/20 = 250 mA) was met by the XIAO charger's normal current. 0.14.1
  requires 4.18 V below C/20 held for a minute, and discards the retained
  gauge from older firmware. It then read 96.4 % (estimate), +247 mA, 0.7 h to
  full.
- The shunt value (0.1 ohm) and capacity (5000 mAh) are assumptions until
  checked against the parts.

### Probe power switch (0.15.0)

- None of the modules on hand can switch the probe: the RS485 board's 12V
  terminal is an input to its regulator, its 5V OUT is USB power only, and a
  disabled MT3608 still passes battery voltage to the probe. An AO3400
  N-MOSFET now switches the MT3608's ground from D10 (GPIO10), with a
  gate pull-down so the probe starts off.
- Verified: the dashboard switch drives GPIO10; continuity between battery -
  and MT3608 OUT- follows it; the probe answers 2.1 s after power-on.
- **The probe costs 28.5 mA** on battery (30 s averages: on -98.0 and -98.1
  mA, off -69.5 mA). The remaining 69.5 mA is the always-awake development
  firmware: Wi-Fi, continuous BLE advertising, SHT45 and INA219.
- With USB connected the INA219 showed no change at all: the charger holds the
  node at 4.2 V and feeds the loads itself. Loads are measured on battery.
- First pH after power-on is wrong (3.3, then 5.6 at +10 s, versus 6.0-6.8
  after long operation); moisture, temperature and EC are right at once.

### Not validated

- 100 consecutive probe reads without a retry, now that the battery is in.
- Soil readings in real soil against a reference; pH against a buffer.
- The production path's soil read and per-reading probe power cycle, and how
  long pH needs after power-on to be trustworthy.
- The battery monitor on battery alone (discharging and time to empty), a
  charge through the IP2312, a completed charge pinning the level to 100 %,
  and the shunt and capacity values against the actual parts.

## 2026-09-27

### Contract v3 switch-over

- Sensor and hub moved to contract v3 together, with no backward
  compatibility. The hub database was backed up first
  (`~/.open-plant-pulse/hub-backup-2026-09-27-schema16.sqlite3`), and the
  16 to 17 migration was rehearsed on a copy of it: 3,905 readings and 7,945
  advertisements kept, integrity and foreign keys clean, 0.05 s.
- 0.16.0 went over the air from the v2 hub, since a v3 hub cannot reach a v2
  sensor, then the hub restarted on v3 and migrated the live database.
- **First v3 reports exposed two gaps.** Report 1 reached the hub as its
  supplementary packet only: the hub held one packet per device while waiting
  for its name, and the supplementary overwrote the main. A Bleak scan from
  the Mac showed both packets arriving, which placed the fault in the hub. And
  forced reports ended unacknowledged: the hub acknowledges once both packets
  are stored, which lands after the sensor's three-second window.
- Fixed by holding up to eight packets per unnamed device, and by accepting the
  acknowledgement for 60 seconds after the window (0.16.1).
- 0.16.1 was installed over the air through the v3 hub in 25 seconds, and the
  hub saw the sensor return on the new version. Forced report 3 was
  acknowledged six seconds after it started. Its reading has soil, air, pH
  5.6, N/P/K 1/3/7, battery 91 % at 4.112 V, and the sensor's timestamp.
  Report IDs continued across the update (1, 2, 3).

- **An interval change waited out the old interval.** The report loop fixed
  its next deadline once per report, so when the hub changed the interval from
  30 minutes to 5 seconds two minutes after a report, the sensor kept waiting
  for the 30-minute deadline while its own page updated every 10 seconds.
  0.16.2 re-reads the interval on every pass, counting from the last report;
  reports then reached the hub every few seconds.
- At a 5-second interval the loss is visible: of reports 4-12, the hub got both
  packets of 4, 10, 11 and 12, only the supplementary of 5, 6 and 9, and
  nothing of 7 and 8. Each report is on air for one window before the next
  replaces it, the Mac's scanning misses some advertisements, and the hub's own
  connections pause advertising. At a 30-minute interval each report is
  repeated for the whole interval.

### Durable delivery (0.17.0, schema 18)

- Contract v3 step 2 landed: an owned sensor queues each report in NVS and
  advertises the oldest until the hub's five-byte acknowledgement names it.
  Every report now carries a supplementary packet so the hub knows when it is
  complete; forced reports are ordinary reports. The migration to schema 18 was
  rehearsed on a copy of the live database (4,024 readings, 118 supplements,
  8,064 advertisements kept) and the live one backed up first
  (`hub-backup-2026-09-27-schema17.sqlite3`).
- On the device at the same 5-second interval that lost about half the reports
  before: reports 164-171 all reached the hub, each acknowledged within
  seconds, none missing. Throughput is one acknowledgement per 6-8 seconds, so
  at 5 seconds a backlog grew by about three reports a minute; at a minute or
  more the queue stays empty.

### Freshness at a 5-second interval (0.18.0-0.18.1)

- One report per connection could not keep up with a report every 5 seconds,
  and advertising the oldest report meant the hub page showed data minutes old.
  The sensor now advertises its newest report, keeps to a fixed schedule with
  advertising windows ending before a report is due, and the hub drains the
  queue in pages over one connection on a gap or every 30 seconds; its latest
  reading is the highest complete report ID.
- Measured over 90 seconds at 250 ms advertising (0.18.0): exactly one report
  every 5 s, queue 0-3, measurement to hub latest median 3.4 s; but 7 of 18
  reports never showed as latest, because the Mac heard only 0-4 advertisements
  of each packet. A Bleak scan showed the main and supplementary packets
  received about equally often, so packet size was not the cause.
- At 50 ms advertising in the always-awake firmware (0.18.1): 17 of 19 reports
  shown, page updates median 5.0 s apart, measurement to hub latest median
  1.8 s (max 4.6 s), queue 0-5. The two not shown were stored complete by the
  drain. Production keeps 250 ms.

- Drains that are due now wait until the report just heard is complete, so
  the connection falls in the quiet part of the cycle instead of across a
  fresh report (hub only). Over three minutes, 16.5 hours into the sensor's
  uptime at a 5-second interval: 34 of 36 reports shown as latest, page updates
  median 5.0 s apart, measurement to hub latest median 3.0 s (max 5.2 s),
  queue 0-6. The uptime and report IDs (about 11,900 reports) agree: no
  restarts, and the queue never filled.

### Not validated

- A full queue on hardware, and a report surviving a power cut while queued.
- Home Assistant decoding the battery objects from the supplementary packet.
- The flash progress bar against a real USB flash.

