# Sensor-to-hub protocol

This directory owns the contract between the battery sensor and desktop hub.
Neither product may change its payload independently.

## BTHome contract version 3

Contract v3 is the only contract. Earlier contracts are not decoded; a sensor
still sending one is reflashed rather than accommodated. It remains unencrypted
BTHome v2 service data under UUID `0xFCD2`; `3` is the Open Plant Pulse
contract version, not a BTHome format version. Every packet begins with
device-info byte `0x40` (regular interval, unencrypted) and lists its objects in
ascending object-ID order.

One **report** is one set of measurements taken together. It always travels as
two packets that share a report ID, advertised alternately: **packet 1** with
the soil and air measurements and the acquisition time, and **packet 2** with
the battery, pH and N/P/K. Neither is optional; a report is complete only when
the hub holds both. A third shape, the **beacon**, carries no report at all.

### Packet 1: soil, air and time

Sizes include the one-byte object ID.

| Wire order | Object | ID | Encoding | Group | Size |
| --- | --- | --- | --- | --- | --- |
| 0 | Device info | `0x40` | first byte, not an object | Required | 1 |
| 1 | Soil temperature | `0x02` | signed 16-bit; factor 0.01 degrees C | Soil | 3 |
| 2 | Air relative humidity | `0x2E` | unsigned 8-bit; 1 % | Air | 2 |
| 3 | Soil moisture | `0x2F` | unsigned 8-bit; 1 % | Soil | 2 |
| 4 | Report ID (`count`) | `0x3E` | unsigned 32-bit | Required | 5 |
| 5 | Air temperature | `0x45` | signed 16-bit; factor 0.1 degrees C | Air | 3 |
| 6 | Acquisition timestamp | `0x50` | unsigned 32-bit Unix seconds | Optional | 5 |
| 7 | Conductivity | `0x56` | unsigned 16-bit; 1 microS/cm | Soil | 3 |

All multi-byte values are little-endian. A group is complete or absent: soil is
`0x02`, `0x2F` and `0x56`; air is `0x2E` and `0x45`. At least one group is
present. A failed source omits its whole group, and the receiver stores null
measurements with an unavailable source status rather than copying earlier
values. Percentages are rounded to the nearest whole point, `0` to `100`. With
everything present the service data after the UUID is 24 bytes, which with the
Flags element and service-data header fills the 31-byte legacy advertisement;
the local name travels in the scan response.

### Packet 2: battery, pH and N/P/K

| Wire order | Object | ID | Encoding | Group | Size |
| --- | --- | --- | --- | --- | --- |
| 0 | Device info | `0x40` | first byte, not an object | Required | 1 |
| 1 | Battery | `0x01` | unsigned 8-bit; 1 % | Battery | 2 |
| 2 | Battery voltage | `0x0C` | unsigned 16-bit; factor 0.001 V | Battery | 3 |
| 3 | Battery charging | `0x16` | unsigned 8-bit; `0` not charging, `1` charging | Battery | 2 |
| 4 | Report ID (`count`) | `0x3E` | unsigned 32-bit | Required | 5 |
| 5 | Soil extras (`raw`) | `0x54` | length byte `0x08`, then the layout below | Soil extras | 10 |

Battery is `0x01`, `0x0C` and `0x16` together or none of them. Charging means
current into the pack above the monitor's idle threshold (15 mA), from whatever
source: the IP2312 or the XIAO's own USB charger. Every report sends packet 2,
even when it carries only the report ID: that is how the hub knows a report is
complete, rather than guessing whether a second packet was lost or never
existed. At most 23 bytes.

BTHome defines no pH or nutrient objects, so they travel in one raw object that
only the hub decodes (Home Assistant shows battery natively and ignores it):

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Layout version, `1` |
| 1 | 1 | pH, factor 0.1 (`0`-`140`) |
| 2 | 2 | Nitrogen, mg/kg |
| 4 | 2 | Phosphorus, mg/kg |
| 6 | 2 | Potassium, mg/kg |

A packet is packet 1 if it carries any of `0x02`, `0x2E`, `0x2F`, `0x45`, `0x50`
or `0x56`, and packet 2 otherwise: `0x01`, `0x0C`, `0x16` and `0x54`, or the
report ID alone. A packet mixing the two sets, missing the report ID, or carrying any
other object is malformed.

### Beacon

The single byte `0x40`, with no objects. A sensor sends it when it has no
report (for example, no sensor is available yet), so that it can still be found
and adopted. A beacon is presence only: nothing is stored from it.

### Byte budget and worked example

A legacy BLE advertisement carries at most 31 bytes. Every contract-v3 packet
spends them the same way:

```text
┌──────────────────────────── 31 bytes ────────────────────────────┐
│ 02 01 06 │ LL 16 D2 FC │ BTHome payload: up to 24 bytes           │
│  Flags   │ service-data│ 40, then objects                         │
│  3 bytes │ header, 4 B │                                          │
└──────────────────────────────────────────────────────────────────┘
```

- **Flags** (length, type `0x01`, value `0x06`) make the sensor discoverable.
- **Service-data header**: length `LL`, AD type `0x16` (service data, 16-bit
  UUID) and the BTHome UUID `0xFCD2`, little-endian `D2 FC`.
- **Payload**: 31 − 3 − 4 = **24 bytes**. It starts with the device-info byte
  `0x40`; each object is its one-byte ID followed by its little-endian value,
  in ascending ID order.

The device-info byte is a bit field defined by BTHome. `0x40` is `0100 0000`:

| Bits | Field | Value in `0x40` | Meaning |
| --- | --- | --- | --- |
| 0 | Encryption | `0` | Not encrypted |
| 1 | Reserved | `0` | |
| 2 | Trigger-based device | `0` | Advertises at regular intervals, not only on events |
| 3-4 | Reserved | `00` | |
| 5-7 | BTHome version | `010` = 2 | BTHome v2 |

Every packet 1, packet 2 and beacon begins with exactly `0x40`; any other value
is malformed. An encrypted contract would set bit 0 (`0x41`) and change the
rest of the payload, and needs a new contract.

The local name `sensor-<DEVICE_ID>` is not in this budget: it travels in the
scan response.

**Packet 1**, fixture `packet1`, 24 of 24 bytes:
`40 02 2e09 2e 2c 2f 64 3e d2040000 45 1001 50 0037b86a 56 2900`

| Bytes | ID | Value | Meaning | Size |
| --- | --- | --- | --- | --- |
| `40` | | `0100 0000` | Device info: BTHome v2, unencrypted, regular interval | 1 |
| `02 2e09` | `0x02` | 0x092E = 2350 x 0.01 | Soil temperature **23.50 C** | 3 |
| `2e 2c` | `0x2E` | 0x2C = 44 | Air humidity **44 %** | 2 |
| `2f 64` | `0x2F` | 0x64 = 100 | Soil moisture **100 %** | 2 |
| `3e d2040000` | `0x3E` | 1234 | **Report ID** | 5 |
| `45 1001` | `0x45` | 0x0110 = 272 x 0.1 | Air temperature **27.2 C** | 3 |
| `50 0037b86a` | `0x50` | 1790457600 | Timestamp **2026-09-26T21:20:00Z** | 5 |
| `56 2900` | `0x56` | 0x29 = 41 | Conductivity **41 uS/cm** | 3 |
| | | | **Total** | **24** |

**Packet 2**, fixture `packet2`, 23 of 24 bytes:
`40 01 60 0c 4010 16 00 3e d2040000 54 08 01 44 0100 0200 0700`

| Bytes | ID | Value | Meaning | Size |
| --- | --- | --- | --- | --- |
| `40` | | `0100 0000` | Device info: BTHome v2, unencrypted, regular interval | 1 |
| `01 60` | `0x01` | 0x60 = 96 | Battery **96 %** | 2 |
| `0c 4010` | `0x0C` | 0x1040 = 4160 x 0.001 | Battery **4.160 V** | 3 |
| `16 00` | `0x16` | 0 | Charging: **no** | 2 |
| `3e d2040000` | `0x3E` | 1234 | **Same report ID**, joining it to packet 1 | 5 |
| `54 08 …` | `0x54` | raw, length 8 | Soil extras, below | 10 |
| | | | **Total** | **23** |

The soil extras, `01 44 0100 0200 0700`:

| Bytes | Value | Meaning | Size |
| --- | --- | --- | --- |
| `01` | 1 | Layout version | 1 |
| `44` | 0x44 = 68 x 0.1 | pH **6.8** | 1 |
| `0100` | 1 | Nitrogen **1 mg/kg** | 2 |
| `0200` | 2 | Phosphorus **2 mg/kg** | 2 |
| `0700` | 7 | Potassium **7 mg/kg** | 2 |

**Room left** with every object present:

| Packet | Used | Free |
| --- | --- | --- |
| Packet 1 | 24 | 0 |
| Packet 2 | 23 | 1 |
| Packet 2, report ID only (`40 3e d3040000`) | 6 | 18 |
| Beacon (`40`) | 1 | 23 |

A new field must fit in the free bytes of the packet it belongs to, including
its one-byte object ID, or replace something; a third packet would spread each
report's airtime more thinly. The sensor advertises a report's two packets
alternately, swapping every 500 ms, every 50 ms while always awake and every
250 ms in the production cycle. The bulk drain does not use advertisements: its
pages travel over a connection and may be up to 512 bytes.

### Identity, report IDs and deduplication

The sensor advertises the complete local name `sensor-<DEVICE_ID>`, where
`DEVICE_ID` is its immutable 48-bit hardware device ID as 12 lowercase
hexadecimal digits. The name is the sensor's identity and stays stable across
resets, hub replacement and platform address changes. Platform-observed
addresses are diagnostics only. The identity is visible and spoofable because
the contract is unauthenticated.

Report IDs start at `1`, increase with every new report, never repeat and never
wrap. Gaps are allowed: the sensor reserves IDs in blocks in flash so that it
writes flash once per block rather than once per report, and after a restart it
continues from the end of the reserved block. `0` is never used.

The hub's key for a report is `(sensor identity, report ID)`. Packets 1 and 2
of one report may arrive in either order and are joined into one reading; a
reading appears once packet 1 is stored, with packet 2's values attached
whenever they arrive. A report is complete when
both are stored, and only a complete report is shown as a sensor's latest
reading, so one whose second packet is still coming does not blank its values. Receiving a packet whose
content matches what is already stored for its key is a duplicate and stores
nothing; this is how repeated advertising of one report is absorbed. The same
key with different content for the same packet kind is a conflict: it is
logged and not stored. Erasing the sensor's flash restarts its report IDs, so
such a sensor must be removed from the hub and adopted again.

### Security

Contract v3 is unencrypted: measurements, identity and presence are observable
and not authenticated. Encryption requires a new contract with BTHome bind-key
provisioning, protected key storage, replacement and recovery, and Home
Assistant interoperability. Keys must never appear in fixtures or logs.

### Fixtures

[`fixtures/bthome-v3.json`](fixtures/bthome-v3.json) records a known packet 1 and
packet 2 pair and a beacon. The sensor's encoder tests and the hub's
decoder tests both check against it.

### Acquisition timestamp

Use the standard BTHome timestamp object `0x50`, not a text datetime or a custom
object. Its value is a POSIX/Unix timestamp: whole seconds since
`1970-01-01T00:00:00Z`, encoded as an unsigned 32-bit little-endian integer and
interpreted in UTC. It carries no timezone offset and does not represent leap
seconds. For example, `2026-09-13T12:00:00Z` is Unix value `1789300800` and is
encoded as `50 40 90 A6 6A` including the object ID.

The Hub converts a present `0x50` value to canonical RFC 3339 UTC text in the form
`YYYY-MM-DDTHH:MM:SSZ` for its API and storage. That value becomes `observed_at`.
The separately recorded `received_at` is always the Hub's reception time and must
not be substituted for acquisition time.

The sensor emits `0x50` only after its clock has been synchronized and passes the
firmware plausibility window from `2024-01-01T00:00:00Z` through the end of 2099.
Deep sleep may retain that clock state; loss of power makes time unknown until a
new trusted synchronization succeeds. A sample acquired while time is unknown
omits `0x50` permanently, including on retries after a later synchronization. It
must not encode zero, estimate from reception time, or rewrite the immutable queued
report. The Hub stores `observed_at` as null for such a report while retaining
`received_at`; this requires a v3 storage migration because the v2 column is not
nullable.

### Durable delivery and acknowledgement

A report is kept until the hub confirms it has stored it. This replaced
one-way delivery, in which a report the hub missed was lost.

**Sensor.** Once the sensor belongs to a hub, each new report is written to a
queue in flash before it is first advertised, and stays there until
acknowledged. The sensor advertises its newest report, both packets
alternately, from the moment it is made, so the hub sees fresh data within a
second or two whatever is still queued behind it; older ones reach the hub by
the drain below. Reports are made on a fixed schedule: an advertising window is
cut short rather than delay a report that is due. A deep-sleeping sensor retries at each
wake. When the queue is full, the sensor makes no new reports and says so on its
console, rather than overwriting an unacknowledged one. An unclaimed sensor has
no hub that could acknowledge anything, so it does not queue: it advertises its
latest report without keeping it. A release by the hub clears the queue.

**Freshness.** The hub stores the newest report straight from its
advertisement, as with any packet, and shows the highest complete report ID as
the latest, so a report backfilled later never displaces a newer one.

**Hub: bulk drain.** The drain makes delivery complete and lets the sensor free
its queue. It is not the freshness path: a connection pauses the sensor's
advertising for a moment, so the hub does not drain after every report. On
hearing a report not yet acknowledged from a sensor it owns, the hub drains
when the report IDs it holds since the last acknowledgement have a gap
(something was missed over the air), when about 30 seconds have passed since
its last drain of that sensor, or on the first such report after the hub
starts. A drain that is due waits until the report just heard is complete,
both packets stored, so the connection falls in the quiet part of the
reporting cycle rather than across a fresh report; if none completes over the
air within 10 seconds it drains anyway, and other connection work waits to ride
on the same connection. Over one connection it drains the whole queue in pages, oldest first, on the
existing read/write characteristic `7f510002-1b15-4c28-9a4a-8d0f4f505000`
(bonded, encrypted):

1. Write the drain request `20`. From then on, until the end token or the
   disconnect, a read of the characteristic returns a queue page instead of
   the device configuration.
2. Read a page. Each page holds up to eight of the oldest queued reports, each
   exactly the two packets the sensor advertises for it.
3. Store every report on the page, through the same duplicate and conflict
   rules as advertised packets.
4. Write the cumulative acknowledgement for the last report stored. The sensor
   removes every queued report up to and including it.
5. Repeat from 2 until a page is empty, then write the end token `22`.

One connection moves any backlog, so draining outpaces reporting whenever the
hub is up: at a report every five seconds, a drain every 30 seconds finds about
six, two pages.
The hub does its configuration and status work on the same connection, before
the drain request or after the end token.

**Queue page** (read, at most 512 bytes):

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | `0x20` |
| 1 | 1 | Report count, `0` to `8` |
| 2 | ... | That many records, oldest first |

Each record is the report ID (4 bytes, unsigned little-endian), packet 1's
length (1 byte) and bytes, then packet 2's length (1
byte) and bytes: the service data exactly as advertised, after the UUID. A page
is fixed until the queue changes, so the reads that assemble a long value see
the same bytes.

**Cumulative acknowledgement** (write, five bytes):

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | `0x21` |
| 1 | 4 | Report ID, unsigned little-endian |

It removes every queued report with an ID up to and including this one. It is
refused, removing nothing, when the ID is `0` or beyond the last report the
sensor has put on a page during this connection: the hub can acknowledge only
what it has read. Naming a report already removed is accepted and changes
nothing. Removal is durable before the sensor answers. A report in conflict is
never acknowledged, so the hub acknowledges only up to the report before it.

**End of drain** (write, one byte): `22`. Reads return the device configuration
again; disconnecting has the same effect.

A report forced from the sensor's console is an ordinary report: it joins the
queue, and the console shows it acknowledged when a cumulative acknowledgement
covers it.

## Change policy

- There is one contract at a time. A new contract replaces the old one in the
  sensor, the hub and the fixtures together; no compatibility is kept.
- Update sensor encoding, hub decoding, replay behavior, and tests together.
- Treat incompatible identity, deduplication, encryption, or object semantics as
  a new contract version.
- Add BTHome fields in ascending object-ID order unless a documented compatibility
  reason requires otherwise.
- Document receiver behavior before emitting an object Home Assistant or the hub
  may not understand.

## Connected-BLE device configuration version 2

BTHome advertisements remain the telemetry contract. During the same bounded
advertising window, firmware also accepts a BLE connection exposing primary service
`7f510001-1b15-4c28-9a4a-8d0f4f505000` and read/write characteristic
`7f510002-1b15-4c28-9a4a-8d0f4f505000`. After an enrolled sensor advertises, the hub
writes pending device configuration and reads the characteristic back. Only an
identical read-back marks the revision applied; failures remain pending for a later
report.

The characteristic value is a canonical binary payload:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Protocol version, currently `2` |
| 1 | 4 | Non-zero configuration revision, unsigned little-endian |
| 5 | 4 | Reporting interval in seconds, unsigned little-endian, `1`–`86400` |
| 9 | 1 | Plant-name byte length, `1`–`80` |
| 10 | 1 | Room byte length, `0`–`80` |
| 11 | variable | Plant name followed by room, UTF-8 without control characters |

Version 1 used a two-byte interval in minutes and accepted only 5 minutes through
24 hours. Version 2 is intentionally incompatible so sub-minute intervals cannot be
misinterpreted by older firmware. After migration, the hub resends enrolled sensor
configuration using version 2.

The sensor validates the entire payload before atomically storing it in NVS. It
renders plant name and room on its diagnostics dashboard and uses the interval for
future awake reports or deep-sleep timer wake-ups until another valid configuration
is applied. Plant profile, thresholds, archival state, and history stay hub-owned.

This prototype characteristic is unencrypted and unauthenticated. Nearby clients
can observe or overwrite configuration. Pairing, authorization, recovery, and
measured connection-window energy are required before production deployment.

## Connected-BLE Wi-Fi credentials version 3

The hub holds one household network for the whole house and sends it to any sensor
whose web console is switched on. The sensor stores it in NVS and joins. Readings
never depend on it: telemetry is BTHome either way, and a sensor with its console
off is fully functional.

This is a third payload on the same read/write characteristic
`7f510002-1b15-4c28-9a4a-8d0f4f505000`, distinguished by its first byte as the
other two are. It is a separate payload rather than an extension of device
configuration for two reasons: that payload already occupies 171 of the roughly
253 bytes an ATT write carries at the negotiated MTU, and Wi-Fi is optional while
plant name and interval are not.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Protocol version, `3` |
| 1 | 4 | Revision, unsigned little-endian |
| 5 | 1 | Console enabled, `0` or `1` |
| 6 | 1 | Network name length, at most 32 |
| 7 | n | Network name |
| 7+n | 1 | Password length, at most 63 |
| 8+n | m | Password |

A disabled console still carries its network, so switching it back on needs
nothing resent. A payload that claims an enabled console with no network name is
refused, as is any payload whose declared lengths do not account for exactly the
bytes received. The sensor never returns the password on a read, and it appears in
no status response, log, or serial output.

> [!IMPORTANT]
> This payload carries a household secret, so it may only be written over a
> bonded, encrypted link. The characteristic requires encryption from firmware
> 0.6.0; earlier firmware accepted reads and writes from anything in range and
> must not be given credentials.

## Development transports

BLE is the only transport. Deterministic tests replay the contract-v3 fixtures
above; there is no side channel a sensor may use instead.
