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

**Advertisements are not covered by the LTK.** BTHome telemetry is broadcast, not
sent over a connection, so link encryption is irrelevant to it. Contract v2 keeps
device-info byte `0x40`, unencrypted, and `protocol/README.md` already records that
measurements, identifiers, and device presence are observable.

They are covered instead by a BTHome bind key, and **that is on by default**.

### Encrypted telemetry, on by default

Without a bind key, a neighbouring hub in range can read this sensor's
measurements, and the sensor inbox means it can enrol them too. The pairing code
stops a stranger *controlling* a sensor; only a bind key stops one *reading* it.
Privacy that every household should have is not a setting, so there is no toggle:
each sensor is given a 16-byte key during onboarding, over the bonded link, and
every advertisement is encrypted from then on. This makes contract v3 the contract
for onboarded sensors.

Three consequences follow:

- **Home Assistant needs the key.** Since encryption is not optional, the hub must
  hand it over: each sensor's key is revealed and copied from that sensor's own
  settings, to paste where Home Assistant asks for a BTHome bind key. A key the hub
  cannot show is a sensor Home Assistant cannot read.
- **The key is per sensor, not per household**, so one exposed key exposes one
  plant's readings. The hub must store it durably, because it is needed to decode
  every advertisement — unlike the Wi-Fi password, it cannot live only in the
  keychain and be re-asked for. Storing it with the sensor record means database
  backups contain keys, which the backup design must account for.
- **The counter must be monotonic across deep sleep**, like the retained packet ID
  already is, or the hub's replay protection rejects the sensor after every wake.

### The 24-byte budget

BTHome v2 encryption sets device-info byte `0x41` and adds a 4-byte counter and a
4-byte MIC, so every advertisement grows by 8 bytes. The counter also gives replay
protection, which the product does not have today. A legacy advertisement holds 31
bytes; flags take 3 and the service-data header 4, leaving **24**. The local name
rides in the scan response and does not compete.

Measured against the current encoder (`bthome_payload.c:49-76`):

| Payload | Plain | Encrypted | Fits in 24 |
| --- | --- | --- | --- |
| Air only, today's sensor | 9 | 17 | yes |
| Air and button | 11 | 19 | yes |
| Air and soil probe | 18 | 26 | no |
| Air, soil, and button | 20 | 28 | no |

Encryption therefore fits the sensor that exists and breaks when the RS-485 probe
lands. **Drop the packet-id object when encrypted**: the counter already provides
deduplication and replay protection, which frees exactly the 2 bytes that bring
the soil payload to 24. That leaves no headroom at all, so a soil sensor cannot
also carry the button event; forced reports stay an air-only diagnostic.

BLE 5 extended advertising would remove the limit, and it is **not viable**.
BTHome tracks it as infeasible because `bleak` does not support it, and `bleak` is
the library this hub and Home Assistant both depend on: the sensor would transmit
correctly and neither receiver would hear it. Revisit only when `bleak` gains
support.

## Delivering credentials

- A **separate versioned payload** on the existing characteristic, not an extension
  of the device-configuration payload. `CONFIG_BT_NIMBLE_ATT_PREFERRED_MTU` is 256,
  giving about 253 usable write bytes; the configuration payload already occupies
  171, and adding SSID and password would overflow it. Separate payloads also keep
  Wi-Fi optional.
- **The household network is hub-level, entered once.** The hub settings page holds
  one network name and password for the home. A sensor is not asked for
  credentials; it carries a single **enable** flag, and the hub sends the stored
  network to any sensor whose flag is on. A customer with four sensors types their
  password once, not four times.
- The hub stores that password in the **operating system keychain**, never in the
  readings database. The database is backed up and exported; a Wi-Fi password must
  not travel with it.
- A **Check network** action scans from the hub and reports whether the network is
  in range, whether it is 2.4 GHz, and what security it uses. It catches the two
  failures customers blame on the sensor: a mistyped or hidden name, and a 5 GHz-only
  network the ESP32-C3 can never join. Reading SSIDs requires Location permission on
  macOS, and a hub may have no Wi-Fi adapter or be wired — the development hub is on
  Ethernet with its Wi-Fi interface unassociated — so the check must degrade to
  saying it could not look rather than claiming a failure.

### Verifying the password

A WPA2 passphrase cannot be checked offline. The pre-shared key is derived from the
passphrase and the network name, and only the four-way handshake with the access
point proves it, so something must associate. The hub cannot be that something: it
would drop its own connection, possibly the one serving the page the customer is
using, and on a wired hub it has no route to try at all.

The sensor is the right test instrument, and the forced report makes it a fast one.
**Test the password** sends the credentials to an enrolled sensor, asks it to join,
and reports the classified result within seconds — the same mechanism that delivers
the first reading during onboarding. It is a genuine end-to-end test rather than a
guess, because the device that must work is the device that tried.

Typing is also worth avoiding entirely. Where the hub's operating system already
holds a password for the network, offer to fill it in from there: on macOS the
keychain, behind the standard authorization prompt; on Linux the NetworkManager
connection. A password that is never retyped cannot be mistyped.
- The sensor stores SSID and password in NVS exactly as it stores device
  configuration today, with `nvs_set_blob` and `nvs_commit`, and never returns the
  password on a read, in `/status`, in the log ring, or over serial.

### The enable flag controls the console

Turning the flag off is not cosmetic. The sensor stops its HTTP server, calls
`esp_wifi_stop()`, and leaves the network; turning it on rejoins at the next
report. The flag therefore controls the only unauthenticated network surface the
product has, and it defaults to **off**. A sensor that nobody has deliberately put
on the network cannot be reached from the network.

Two consequences to design for:

- Wi-Fi today is compile-time (`CONFIG_OPP_WEB_UI_ENABLED`). This flag makes it a
  runtime decision, so `web_ui_start()` must become startable and stoppable rather
  than a one-way call made once at boot.
- **SNTP clock synchronisation shares that radio.** With Wi-Fi off the clock drifts
  and sample timestamps stop being wall-clock accurate, falling back to the
  monotonic ordering the firmware already keeps. The interface must say so where
  the flag is turned off, rather than letting a customer discover it later.
- Delivery reuses the existing desired/acknowledged revision mechanism, so a sensor
  that is asleep or out of range receives credentials after its next report.
- The sensor reports the join result back: joined with an address, or a classified
  failure. Today the firmware reconnects silently forever with no reason code
  (`web_ui.c:621-627`); onboarding must distinguish wrong password, SSID not found,
  and association or DHCP timeout, or the customer has nothing to act on.

Wi-Fi remains optional throughout. A customer who skips it has a fully working
sensor, because telemetry is BLE.

### The first reading arrives during onboarding

Onboarding must not end by telling a customer to wait for the next reporting
interval. At a 30-minute interval that leaves them with a sensor they have no
evidence works, and no way to tell a successful setup from a broken one.

The last step of enrolment is therefore a **forced report**, requested by the hub
over the link it is already connected on. The firmware mechanism exists:
`opp_force_report_request()` emits a report immediately, carries the BTHome button
event, and exposes a request/packet token the hub acknowledges after durable
ingestion. It is only wired to the web console today, which a factory-fresh sensor
cannot reach; onboarding needs the same request over the bonded connection.

The budget is seconds, not minutes: an SHT45 acquisition, the bounded 3-second
advertising window, and ingestion. **Target a first stored reading within 30
seconds of the customer finishing the flow**, and show the measured values rather
than a promise.

The failure path matters as much. A forced report fails with `no valid sensor
sample` when no sensor has produced a reading, which is a real state seen on the
bench with an SHT45 that was not responding. Onboarding must take a fresh sample
first, and if none is available, say the sensor is not reading its hardware rather
than spinning. A customer who finishes onboarding and sees no reading should be
told which of the two happened.

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

The customer journey is the riskiest part of this design and the cheapest to
change, so it comes first. The hub is Python and iterates in seconds; firmware
iterates in build, flash, and hardware cycles. Phase 1 therefore produces a
complete, clickable experience driven by a simulated sensor, and its output is the
contract the later firmware phases implement.

**Phase 1, hub interface.** A dedicated management page, separate from the
monitoring dashboard, covering sensors, Wi-Fi, and keys. A separate guided
onboarding flow for adding one new sensor. Both driven end to end by a simulated
unclaimed sensor through the existing replay and simulation paths, with no
firmware change and no hardware.

Phase 1 is complete when the journey can be walked and reviewed, and when it has
produced an explicit list of the states, fields, and failure reasons the firmware
must expose. Everything below implements that list.

Scope the management page honestly. The hub never sees the LTK, which lives inside
the host Bluetooth stack, and it must not store the passkey, which is on the
device label. What it manages is the bonded sensor, forget and transfer, Wi-Fi
credentials as write-only, and later the BTHome bind key.

**Phase 2, secure the link.** Secure Connections with bonding and passkey,
persistent bonds, encrypted characteristic, factory passkey storage. No new
features. Prerequisite for everything below.

**Phase 3, enrolment on real hardware.** Single-bond enforcement, onboarding and
onboarded state in `/status`, and the passkey step wired to whatever the host
platform actually allows.

**Phase 4, credentials.** Versioned Wi-Fi payload into NVS, delivery through the
existing revision mechanism, classified join results shown in the flow built in
phase 1.

**Phase 5, reset.** Hub-initiated forget, power-cycle counter, documented transfer
procedure.

### Platform constraint on the passkey step

`bleak` cannot initiate pairing on macOS: its CoreBluetooth backend raises
`NotImplementedError("Pairing is not available in Core Bluetooth.")`. Pairing is
triggered implicitly by first access to an encrypted characteristic, and macOS
presents its own passkey dialog, attributed to the hub application. On Linux,
BlueZ does expose pairing and an agent can supply the passkey directly.

The onboarding flow must therefore not own a passkey field on macOS. It instructs
the customer to expect a system dialog, tells them where the code is printed, and
polls for the bond. Phase 1 must design this state explicitly rather than assume a
form field, and the two platforms need separate validation.

## Validation

Host checks, build, flash, and device behaviour are separate claims.

- [ ] The whole onboarding flow can be walked against a simulated sensor, with no
      hardware, and is covered by host tests.
- [ ] The flow is validated on macOS and on Linux, whose passkey steps differ.
- [ ] A factory-fresh sensor reaches onboarded with no router present.
- [ ] Enrolment fails with a wrong passkey.
- [ ] An unbonded central can neither read nor write the configuration
      characteristic.
- [ ] A second hub cannot bond while the sensor is onboarded.
- [ ] The bond survives a power cycle and a deep-sleep wake.
- [ ] The sensor joins Wi-Fi from hub-delivered credentials and survives a reset.
- [ ] A wrong password is reported as a wrong password, not a timeout.
- [ ] A first reading is stored within 30 seconds of finishing the flow, measured,
      not assumed.
- [ ] A sensor whose hardware is not reading says so at the end of onboarding
      instead of waiting.
- [ ] Turning the enable flag off stops the sensor's HTTP server and removes it
      from the network; turning it on restores both.
- [ ] The household password is in the keychain and in no database row, backup, or
      export.
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
