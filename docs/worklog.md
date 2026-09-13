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
- Left SHT45 and RS-485 soil-probe acquisition unimplemented until hardware behavior and register details are validated.

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
- Added immediate browser previews while keeping persistence behind an explicit Save action.
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

### Remaining work

- Implement and validate the SHT45 driver and ambient temperature/humidity display.
- Validate the purchased RS-485 soil probe register map and implement bounded acquisition.
- Add battery voltage, charge state, Wi-Fi signal, reset reason, and stale-reading health indicators.
- Complete versioned status/log APIs and log controls described in the web UI proposal.
- Design authentication or a physical-presence policy before exposing sensitive maintenance operations.
- Revisit the flash header and partition table before adding OTA or larger persistent assets.
- Return the production firmware to a BLE/deep-sleep lifecycle after bench diagnostics are complete.
