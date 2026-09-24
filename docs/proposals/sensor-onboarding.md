# Sensor onboarding

## Status

Proposed. Nothing implemented.

## Goal

A customer brings home a new sensor that has never seen a router or a hub. Two
things must happen, with no terminal, no reflash, and no app:

1. The sensor registers with exactly one hub.
2. The sensor receives the household Wi-Fi credentials.

## States

The sensor has two states. The state is **derived from whether a BLE bond exists**,
not stored separately, so it cannot disagree with reality.

| State | Condition | Behaviour |
| --- | --- | --- |
| Onboarding | no bond | Advertise connectably, accept one pairing attempt, refuse all configuration access |
| Onboarded | bond present | Advertise telemetry, accept connections only from the bonded hub, allow configuration over the encrypted link |

A factory-fresh sensor is in onboarding. A reset returns it there. There is no
other way back, which is what makes "one sensor, one hub" true rather than
merely conventional.

`/status` exposes the state so the console and the hub inbox agree.

## Key decision

**The hub is the onboarding interface. The sensor never raises an access point.**

BLE already connects them and needs no router. The sensor advertises connectably
today (`BLE_GAP_CONN_MODE_UND`), the hub already discovers unenrolled sensors into
an inbox, and it already writes configuration over a characteristic and verifies
it by read-back. This removes the access point, the captive portal, the DNS
redirects, and the hotspot-switching step from the customer's experience, along
with their flash cost. Firmware 0.5.0 left about 17% of the app partition free and
there is no OTA.

## Handshake

Use BLE LE Secure Connections. Do not design a key exchange.

LE Secure Connections performs ECDH on P-256: both sides exchange public keys and
each independently **derives** the same Long Term Key. **The LTK is never
transmitted**, so a passive listener that captures the entire handshake cannot
compute it. `CONFIG_BT_NIMBLE_SM_SC` is already enabled in the build; the firmware
simply never configures or uses it. Legacy pairing must not be accepted as a
fallback, because it does distribute keys and is recoverable by a sniffer.

### Proving ownership, and defeating an active attacker

Just Works pairing has no man-in-the-middle protection, and the ESP32-C3 has no
display or keypad. The answer is a **six-digit passkey generated once at first
boot and printed on the device label**, entered into the hub at enrolment with
`sm_io_cap = BLE_HS_IO_DISPLAY_ONLY`.

One mechanism solves two problems. It gives real man-in-the-middle protection, and
it proves the person enrolling physically holds the device, so a neighbour's hub
cannot claim the sensor and the customer cannot accidentally enrol the neighbour's.
It also avoids depending on a button, which matters: the firmware reads no GPIO
today, and the enclosure exposes none.

The passkey is device identity, not a bond. It lives in its own NVS namespace and
**a reset must not erase it**.

### Firmware configuration

```c
ble_hs_cfg.sm_sc          = 1;   /* Secure Connections, not legacy */
ble_hs_cfg.sm_bonding     = 1;   /* keep the derived key */
ble_hs_cfg.sm_mitm        = 1;   /* require the passkey */
ble_hs_cfg.sm_io_cap      = BLE_HS_IO_DISPLAY_ONLY;
ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
```

and the configuration characteristic re-declared from today's bare
`BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_WRITE` (`bthome_broadcaster.c:99`) to
`READ_ENC | WRITE_ENC`, so NimBLE refuses access on an unencrypted link.

## What the encryption does and does not cover

**Connections are encrypted by the controller.** Application code never touches the
LTK and never calls an encryption function. After bonding, each new connection
starts unencrypted, the hub requests encryption from the stored key, and only then
does the `_ENC` flag allow configuration access. That brief unencrypted window is
exactly why the flag matters.

**Advertisements are not covered.** BTHome telemetry is broadcast, not sent over a
connection, so the LTK is irrelevant to it. Contract v2 keeps device-info byte
`0x40`, unencrypted, and `protocol/README.md` already records that measurements,
identifiers, and device presence are observable.

Encrypting telemetry needs a separate 16-byte BTHome bind key, which is a contract
v3 decision. Onboarding is the natural moment to deliver one, so **the credential
payload must leave room for a 16-byte key** to avoid a second migration later.

## Delivering credentials

- A **separate versioned payload** on the existing characteristic, not an extension
  of the device-configuration payload. `CONFIG_BT_NIMBLE_ATT_PREFERRED_MTU` is 256,
  giving about 253 usable write bytes; the configuration payload already occupies
  171, and adding SSID and password would overflow it. Separate payloads also keep
  Wi-Fi optional.
- The sensor stores SSID and password in NVS exactly as it stores device
  configuration today, with `nvs_set_blob` and `nvs_commit`, and never returns the
  password on a read, in `/status`, in the log ring, or over serial.
- The hub does not persist the password in SQLite. It holds it only for the write
  and asks again if a sensor needs it later.
- Delivery reuses the existing desired/acknowledged revision mechanism, so a sensor
  that is asleep or out of range receives credentials after its next report.
- The sensor reports the join result back: joined with an address, or a classified
  failure. Today the firmware reconnects silently forever with no reason code
  (`web_ui.c:621-627`); onboarding must distinguish wrong password, SSID not found,
  and association or DHCP timeout, or the customer has nothing to act on.

Wi-Fi remains optional throughout. A customer who skips it has a fully working
sensor, because telemetry is BLE.

## Reset and transfer to a new hub

Transferring a sensor to a different hub requires a reset. `MAX_BONDS` is set to 1
and pairing is refused while a bond exists, so no second hub can attach quietly.

A reset **erases** the BLE bond (`ble_store_clear()`), the `device_cfg` namespace,
and the Wi-Fi credentials (`esp_wifi_restore()` and the stored entries). It
**keeps** the factory passkey, the firmware, and radio calibration. Bond and
configuration must be cleared together; a sensor holding the old hub's plant name
while reading as unbonded is a confusing half-state.

Two paths, because each covers the other's failure:

**From the hub**, a "forget this sensor" action over the encrypted link. This is the
normal case.

**Physically, by power-cycling five times.** The customer uses the RESET button,
or pulls power. A counter in NVS increments on each boot and is cleared after ten
seconds of uptime, so an ordinary power-up never accumulates. This is the recovery
path for a hub that has been lost or replaced, without which a sensor bonded to a
dead hub is unusable.

The counter must only advance for `ESP_RST_POWERON` and `ESP_RST_EXT`. A deep-sleep
wake reports `ESP_RST_DEEPSLEEP`, and the production lifecycle produces thousands of
those a month.

The RESET button alone cannot do this work: it pulls the chip's reset line, so
nothing is erased and no firmware runs to handle it. GPIO9, the BOOT button, is a
usable long-press alternative on the bench, but it is a strapping pin — holding it
through a reset enters download mode instead of the application — and the enclosure
exposes no opening for it.

Reset needs no indicator. The sensor returns to onboarding and reappears in the
hub's inbox, which is the customer's confirmation.

## Required build changes

| Setting | Now | Needed |
| --- | --- | --- |
| `CONFIG_BT_NIMBLE_NVS_PERSIST` | not set | **enabled** |
| `CONFIG_BT_NIMBLE_MAX_BONDS` | 3 | 1 |
| `CONFIG_BT_NIMBLE_SM_SC` | enabled | unchanged |

`NVS_PERSIST` is the one that silently breaks everything. Without it NimBLE keeps
bonds in RAM, so every deep-sleep wake loses the bond and the sensor falls back to
onboarding roughly every reporting interval.

## Plan

**Phase 1, secure the link.** Secure Connections with bonding and passkey,
persistent bonds, encrypted characteristic, factory passkey storage. No new
features. Prerequisite for everything else.

**Phase 2, enrolment.** Passkey entry in the hub inbox, single-bond enforcement,
onboarding and onboarded state in `/status`.

**Phase 3, credentials.** Versioned Wi-Fi payload into NVS, hub UI, delivery
through the existing revision mechanism, classified join results.

**Phase 4, reset.** Hub-initiated forget, power-cycle counter, documented transfer
procedure.

## Validation

Host checks, build, flash, and device behaviour are separate claims.

- [ ] A factory-fresh sensor reaches onboarded with no router present.
- [ ] Enrolment fails with a wrong passkey.
- [ ] An unbonded central can neither read nor write the configuration
      characteristic.
- [ ] A second hub cannot bond while the sensor is onboarded.
- [ ] The bond survives a power cycle and a deep-sleep wake.
- [ ] The sensor joins Wi-Fi from hub-delivered credentials and survives a reset.
- [ ] A wrong password is reported as a wrong password, not a timeout.
- [ ] Five rapid power cycles reset the sensor; four do not; a day of deep-sleep
      wakes does not.
- [ ] The passkey survives a reset.
- [ ] No password appears in any hub database row, sensor response, log, or serial
      output.
- [ ] App partition free space recorded before and after.
- [ ] The production deep-sleep build is unchanged in size and behaviour.

## Open questions

- How is the passkey printed for a self-built sensor? It is generated at first
  boot, so the builder must read it from serial before the enclosure is closed.
- Should telemetry encryption (BTHome bind key, contract v3) be delivered by this
  same handshake, or deferred?
- Is flash encryption in scope? Without it, the stored credentials, bond, and
  passkey are readable by anyone holding the board.
