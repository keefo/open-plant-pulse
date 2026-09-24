# Sensor Wi-Fi onboarding

## Status

Proposed. Nothing implemented.

## Problem

SSID and password are Kconfig constants compiled into the image
(`sensor/firmware/main/web_ui.c:696-706`). Changing network needs ESP-IDF, a USB
cable, and a reflash. Every failure looks identical: the disconnect handler
reconnects immediately, with no backoff and no reason code logged
(`web_ui.c:621-627`), so a wrong password, an absent SSID, and a 5 GHz-only
network are indistinguishable.

Sensor Wi-Fi serves the diagnostics console and SNTP only. Production telemetry
stays BLE and deep sleep, and this proposal does not change that.

## Customer experience

1. A board with no stored network raises an access point, `PlantPulse-<MAC4>`.
2. The customer joins it from a phone; the captive portal opens the setup page.
3. The page lists scanned networks. They pick one and type the password.
4. The board reports success, or the specific reason it failed, then closes the
   access point and joins.
5. If the stored network later disappears past the retry window, the access point
   returns on its own.

## Design

Reuse what the firmware already has. No new provisioning stack.

- Credentials move from Kconfig into NVS, as a versioned record alongside the
  existing device configuration. Kconfig values seed the first boot on bench
  builds only.
- The setup page is served by the existing `esp_http_server` and the existing
  embedded asset pipeline.
- Endpoints: `GET /setup`, `GET /setup/networks` backed by `esp_wifi_scan_start`,
  `POST /setup/connect`, `GET /setup/status`, `POST /setup/forget`, plus redirects
  from the vendor captive-portal probe paths.
- `WIFI_MODE_AP` while onboarding, `WIFI_MODE_STA` afterwards. Never both.
- Classify `wifi_event_sta_disconnected_t.reason` into wrong password, SSID not
  found, association timeout, and DHCP timeout, and show that to the customer.
- Drop the hardcoded `WIFI_AUTH_WPA2_PSK` threshold so open and WPA-only networks
  can be selected.

Rejected: ESP-IDF `wifi_provisioning` over BLE. It needs protocomm and Espressif's
phone app, and costs flash the partition does not have.

Rejected: writing credentials over the existing BLE configuration characteristic.
It would work and is nearly free, but only the hub can drive it, so it cannot
onboard a board the hub has never met.

Flash budget is the main risk. Firmware 0.5.0 left about 17% of the app partition
free and there is no OTA, so a bad image needs USB recovery. Measure the cost of
AP mode, scanning, and the setup page before committing to phase 3.

## Security

- The setup access point uses WPA2 with a per-device password derived from the
  MAC, printed on the board label and in the serial log. An open AP would expose
  the home password to anyone in range.
- The password is written to NVS only. It must never appear in `/status`, the log
  ring, or serial output.
- Setup endpoints exist only in the diagnostics build, never in the production
  lifecycle build.
- Flash is unencrypted today, so anyone holding the board can read the stored
  password. Decide on flash encryption before this ships.

## Plan

**Phase 1, diagnosable failures.** Log the disconnect reason code, add reconnect
backoff, expose connection state in `/status`. No onboarding yet, useful alone.

**Phase 2, credentials in NVS.** Versioned record, Kconfig seeds first boot.

**Phase 3, setup access point.** AP mode, scan endpoint, setup page, captive-portal
redirects, automatic return to setup after sustained failure.

**Phase 4, recovery.** Forget network, a documented button or boot recovery action,
and a customer-facing setup document.

## Validation

Host checks, build, flash, and device behaviour are separate claims.

- [ ] A wrong password reports a wrong password, not a timeout.
- [ ] An iPhone and an Android handset each open the portal automatically.
- [ ] Credentials survive a reflash without NVS erase.
- [ ] No password appears in `/status`, `/logs`, or serial output.
- [ ] App partition free space recorded before and after.
- [ ] The production lifecycle build is unchanged in size and behaviour.

## Open questions

- Does the sensor need Wi-Fi beyond the bench at all, given BLE telemetry? If not,
  phase 1 may be the whole job.
- Flash encryption before or after this work?
