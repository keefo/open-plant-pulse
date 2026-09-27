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

One **report** is one set of measurements taken together. It travels as up to
two packets that share a report ID: a **main** packet with the core
measurements and, when there is anything for it, a **supplementary** packet
with battery, the remaining soil values and the forced-report marker. Both are
advertised alternately in the same window. A third shape, the **beacon**,
carries no report at all.

### Main packet

| Wire order | Object | ID | Encoding | Group |
| --- | --- | --- | --- | --- |
| 1 | Soil temperature | `0x02` | signed 16-bit; factor 0.01 degrees C | Soil |
| 2 | Air relative humidity | `0x2E` | unsigned 8-bit; 1 % | Air |
| 3 | Soil moisture | `0x2F` | unsigned 8-bit; 1 % | Soil |
| 4 | Report ID (`count`) | `0x3E` | unsigned 32-bit | Required |
| 5 | Air temperature | `0x45` | signed 16-bit; factor 0.1 degrees C | Air |
| 6 | Acquisition timestamp | `0x50` | unsigned 32-bit Unix seconds | Optional |
| 7 | Conductivity | `0x56` | unsigned 16-bit; 1 microS/cm | Soil |

All multi-byte values are little-endian. A group is complete or absent: soil is
`0x02`, `0x2F` and `0x56`; air is `0x2E` and `0x45`. At least one group is
present. A failed source omits its whole group, and the receiver stores null
measurements with an unavailable source status rather than copying earlier
values. Percentages are rounded to the nearest whole point, `0` to `100`. With
everything present the service data after the UUID is 24 bytes, which with the
Flags element and service-data header fills the 31-byte legacy advertisement;
the local name travels in the scan response.

### Supplementary packet

| Wire order | Object | ID | Encoding | Group |
| --- | --- | --- | --- | --- |
| 1 | Battery | `0x01` | unsigned 8-bit; 1 % | Battery |
| 2 | Battery voltage | `0x0C` | unsigned 16-bit; factor 0.001 V | Battery |
| 3 | Button event | `0x3A` | unsigned 8-bit; `0x01` press | Forced report |
| 4 | Report ID (`count`) | `0x3E` | unsigned 32-bit | Required |
| 5 | Soil extras (`raw`) | `0x54` | length byte `0x08`, then the layout below | Soil extras |

Battery is `0x01` and `0x0C` together or neither. `0x3A` appears only in the
report a user forced from the sensor's console. At least one group besides the
report ID is present; a report with nothing for the supplementary packet sends
only the main one. At most 23 bytes.

BTHome defines no pH or nutrient objects, so they travel in one raw object that
only the hub decodes (Home Assistant shows battery natively and ignores it):

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Layout version, `1` |
| 1 | 1 | pH, factor 0.1 (`0`-`140`) |
| 2 | 2 | Nitrogen, mg/kg |
| 4 | 2 | Phosphorus, mg/kg |
| 6 | 2 | Potassium, mg/kg |

A packet is main if it carries any of `0x02`, `0x2E`, `0x2F`, `0x45`, `0x50` or
`0x56`, and supplementary if it carries any of `0x01`, `0x0C`, `0x3A` or `0x54`.
A packet mixing the two sets, missing the report ID, or carrying any other
object is malformed.

### Beacon

The single byte `0x40`, with no objects. A sensor sends it when it has no
report (for example, no sensor is available yet), so that it can still be found
and adopted. A beacon is presence only: nothing is stored from it.

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

The hub's key for a report is `(sensor identity, report ID)`. Main and
supplementary packets of one report may arrive in either order and are joined
into one reading; a reading appears once its main packet is stored, with
supplementary values attached whenever they arrive. Receiving a packet whose
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

[`fixtures/bthome-v3.json`](fixtures/bthome-v3.json) records a known main and
supplementary packet pair and a beacon. The sensor's encoder tests and the hub's
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

### Durable delivery and acknowledgement (step 2, not implemented)

Contract v3 is being introduced in two steps. The telemetry above is step 1.
Durable delivery below is step 2; until it lands, a report the hub misses is
lost, and repeated advertising is the only protection.

The sensor persists each complete report before its first advertisement and keeps
it until acknowledged. It advertises the oldest unacknowledged report first and
retries the identical report with bounded exponential backoff and jitter. Queue
capacity exhaustion must stop new sampling and raise a local diagnostic rather
than silently overwrite an unacknowledged report.

The Hub's durable uniqueness key is `(sensor identity, enrollment, report ID)`. It
commits the report and reception metadata before acknowledging it. Receiving the
same key and identical immutable content is a successful duplicate and must produce
the same acknowledgement; receiving the same key with different content is a
conflict and must not be acknowledged.

Connected-BLE report acknowledgement version 3 is a five-byte token on the existing
read/write characteristic (version 2 is the forced-report acknowledgement):

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Acknowledgement protocol version, `3` |
| 1 | 4 | Report ID, unsigned little-endian |

Only an exact acknowledgement for a durably stored report removes that report from
the sensor queue. A duplicate, delayed, mismatched, or out-of-order token must not
remove another report. Sensor deletion and queue-head advancement must be durable
before the next report is advertised.

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

## Connected-BLE forced-report acknowledgement version 2

The always-awake diagnostics firmware can request an immediate report from its
console, bypassing the current reporting interval without changing the next
scheduled deadline. That report's supplementary packet carries the button event
`0x3A` with press `0x01`. The hub first durably ingests the report, then
connects to the existing service and uses its read/write characteristic
`7f510002-1b15-4c28-9a4a-8d0f4f505000` to acknowledge the exact request.
Payload version and length distinguish the token from device configuration.

The characteristic value is a canonical nine-byte token:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Protocol version, `2` |
| 1 | 4 | Non-zero force-report request ID, unsigned little-endian |
| 5 | 4 | Report ID of the forced report, unsigned little-endian |

While a request awaits acknowledgment, firmware returns the token instead of
device configuration when the characteristic is read. The hub verifies that its
report ID matches the report it stored with the button event, and writes the
identical token back. Only that exact write marks the request acknowledged;
subsequent reads return device configuration normally. A missing, mismatched or
late acknowledgment is not reported as success. Repeated callbacks for one
advertisement burst do not trigger repeated hub connections.

This acknowledgment is limited to user-initiated reports in the always-awake
diagnostics runtime. Scheduled and production telemetry remain one-way until
durable delivery (step 2) lands.

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
