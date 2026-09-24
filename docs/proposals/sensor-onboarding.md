# Sensor onboarding

## Status

Proposed. Nothing implemented.

## Goal

A customer brings home a new sensor that has never seen a router or a hub. Two
things must happen:

1. The sensor registers with the hub.
2. The sensor receives the household Wi-Fi credentials.

Both must work out of the box, with no terminal, no reflash, and no app.

## Key decision

**The hub is the onboarding interface. The sensor never raises an access point.**

BLE is already the path between them, and it needs no router. The sensor
advertises connectably today (`BLE_GAP_CONN_MODE_UND`), the hub already discovers
unenrolled sensors into an inbox, and it already writes plant name, room, and
reporting interval over a connected characteristic and verifies them by read-back.
Wi-Fi credentials are one more payload on a channel that exists and works.

This removes the access point, the captive portal, the DNS redirects, and the
hotspot-switching step from the customer's experience. It also removes their flash
cost, which matters: firmware 0.5.0 left about 17% of the app partition free and
there is no OTA.

## The blocker

The link that would carry the password is wide open today.

- The configuration characteristic is declared `BLE_GATT_CHR_F_READ |
  BLE_GATT_CHR_F_WRITE` (`bthome_broadcaster.c:99`). No encryption, no
  authentication.
- The firmware sets no `ble_hs_cfg.sm_*` fields at all. There is no pairing, no
  bonding, and no LE Secure Connections.
- Advertising is undirected connectable, so any central in range can connect and
  write.

`docs/hub.md` already warns that this service "is unauthenticated and must not be
treated as secure provisioning". Sending a Wi-Fi password over it as it stands
would hand that password to anyone in range with a phone, and would let a
neighbour reconfigure the sensor. **Securing the link is a prerequisite for goal 2,
not a later hardening pass.**

## Customer experience

1. The customer powers the sensor. It advertises. No router is involved.
2. The hub's inbox shows it as a new sensor, with signal strength.
3. The customer clicks Enrol. The hub asks them to **press the button on the
   sensor** to prove the device is theirs and in their hands.
4. The sensor and hub bond. The link becomes encrypted.
5. The customer names the plant and room, and enters the Wi-Fi network and
   password on the same page.
6. The hub writes all of it over the bonded link. The sensor stores it in NVS and
   joins Wi-Fi.
7. The hub shows the result: enrolled, and either the sensor's console address or
   the specific reason the Wi-Fi join failed.

Wi-Fi is optional throughout. A customer who skips step 5 has a fully working
sensor, because telemetry is BLE.

## Design

### Proving ownership

Proximity alone is not proof: a neighbour's sensor can be in range. Enrolment
requires a physical action on the device. The hub accepts a bond only from a
sensor that reported a button press inside a short window, carried as the BTHome
button event (`0x3A`, press `0x01`) that the forced-report feature already emits.

The event exists, but nothing physical produces it. `app_main.c:168` sets it only
when a forced report is requested from the web console, and that console requires
Wi-Fi, which a factory-fresh sensor does not have. **This design therefore depends
on firmware gaining a real GPIO button read**, which does not exist today. The
XIAO ESP32-C3's BOOT button on GPIO9 is usable as an input after boot and is the
obvious candidate for bench work; final hardware needs a deliberate answer.

If no button is acceptable on final hardware, the fallback is a secret printed on
the device, typed into the hub at enrolment. That costs a label and a packaging
step, and is the only other way to prove possession rather than mere proximity.

### Securing the link

Enable NimBLE LE Secure Connections with bonding, and re-declare the
configuration characteristic as `WRITE_ENC | READ_ENC` so an unencrypted client
cannot touch it. The sensor has no display or keypad, so pairing is Just Works,
which does not resist an active man-in-the-middle. The button press bounds that
exposure to the moment of enrolment rather than leaving the characteristic open
forever, which is the realistic improvement available on this hardware.

Bonds persist in NVS. A re-enrolment or a hub replacement must be able to clear
them.

### Delivering credentials

- A new versioned payload on the existing characteristic, distinguished by
  version and length exactly as the forced-report token already is. No new
  characteristic, so no GATT service-cache migration.
- The sensor writes SSID and password to NVS and never returns the password on a
  read, in `/status`, in the log ring, or over serial.
- The hub does not store the password in SQLite. It holds it only for the duration
  of the write, and asks again if a sensor needs it later.
- Delivery reuses the existing desired/acknowledged revision mechanism, so a
  sensor that is asleep or out of range gets the credentials after its next
  report.

### Reporting the result

The sensor reports its Wi-Fi state back to the hub on the next connection:
joined with an address, or a classified failure. The current firmware reconnects
silently forever with no reason code (`web_ui.c:621-627`); onboarding must
distinguish wrong password, SSID not found, and association or DHCP timeout, or
the customer has nothing to act on.

## Plan

**Phase 1, secure the link.** Bonding and LE Secure Connections, encrypted
characteristic, bond clearing. No new features. Prerequisite for everything else.

**Phase 2, ownership-proving enrolment.** A physical GPIO button read in firmware,
then button-press confirmation in the hub inbox with a bounded acceptance window.

**Phase 3, credentials.** Versioned Wi-Fi payload into NVS, hub UI, delivery
through the existing revision mechanism.

**Phase 4, feedback.** Classified join results reported to the hub, console
address shown, retry and forget-network actions.

## Validation

Host checks, build, flash, and device behaviour are separate claims.

- [ ] A factory-fresh sensor reaches enrolled with no router present.
- [ ] Enrolment is refused without the button press.
- [ ] An unbonded central cannot read or write the configuration characteristic.
- [ ] The sensor joins Wi-Fi from hub-delivered credentials and survives a reset.
- [ ] A wrong password is reported as a wrong password, not a timeout.
- [ ] The password appears in no hub database row, sensor response, log, or serial
      output.
- [ ] App partition free space recorded before and after.
- [ ] The production deep-sleep build is unchanged in size and behaviour.

## Open questions

- What is the sensor's button on final hardware? The enrolment gate depends on it,
  the firmware reads no GPIO button today, and the enclosure exposes none.
- Should a sensor ever be enrollable without the button, for a customer who cannot
  reach the device?
- Is flash encryption in scope? Without it, stored credentials are readable by
  anyone holding the board.
