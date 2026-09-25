# Sensor-to-hub protocol

This directory owns the contract between the battery sensor and desktop hub.
Neither product may change its payload independently.

## Historical BTHome contract version 1

BLE/BTHome is the first hub-ingestion transport. The existing fixture uses
unencrypted BTHome v2 service data under UUID `0xFCD2`:

| Order | Object | ID | Encoding |
| --- | --- | --- | --- |
| 1 | Soil temperature | `0x02` | signed 16-bit little-endian; factor 0.01 degrees C |
| 2 | Moisture | `0x14` | unsigned 16-bit little-endian; factor 0.01% |
| 3 | Conductivity | `0x56` | unsigned 16-bit little-endian; 1 microS/cm |

The first service-data byte is `0x40`: BTHome v2, regular interval, unencrypted.
The [shared fixture](fixtures/bthome-v2.json) records a known payload for sensor
encoder, hub decoder, and Home Assistant compatibility tests.

Version 1 has no stable identity or packet ID. The decoder retains it only for the
historical fixture and returns `unenrolled-bthome` with sequence `0`; the Bleak
ingestion path does not accept it for durable multi-sensor collection.

## Implemented BTHome contract version 2

Contract v2 uses standard BTHome objects in ascending object-ID order:

| Order | Object | ID | Encoding | Source |
| --- | --- | --- | --- | --- |
| 1 | Packet ID | `0x00` | unsigned 8-bit | Wake-cycle sample |
| 2 | Soil temperature | `0x02` | signed 16-bit; factor 0.01 degrees C | RS485 soil probe |
| 3 | Air relative humidity | `0x03` | unsigned 16-bit; factor 0.01% | SHT45 |
| 4 | Soil moisture | `0x14` | unsigned 16-bit; factor 0.01% | RS485 soil probe |
| 5 | Button event | `0x3A` | unsigned 8-bit; `0x01` press | Forced-report request only |
| 6 | Air temperature | `0x45` | signed 16-bit; factor 0.1 degrees C | SHT45 |
| 7 | Conductivity | `0x56` | unsigned 16-bit; 1 microS/cm | RS485 soil probe |

`0x02` and `0x45` intentionally distinguish the two temperature sources without
positional inference. A soil sample is complete only when `0x02`, `0x14`, and
`0x56` are present. An air sample is complete only when `0x03` and `0x45` are
present. A failed source omits its complete object group; receivers store null
measurements and an unavailable source status rather than copying earlier values.
Packets with incomplete source groups are malformed.

### Identity and deduplication

The sensor advertises the complete local name `sensor-<DEVICE_ID>`, where `DEVICE_ID`
is its immutable 48-bit hardware device ID encoded as 12 lowercase hexadecimal
digits. The advertised name and hub canonical ID use the same
`sensor-<lowercase-device-id>` format. Previous uppercase `sensor-<DEVICE_ID>` and
legacy uppercase `OPP-<DEVICE_ID>` names are accepted during upgrades and
canonicalized to the same lowercase hyphenated identity. The name is
sensor-owned, so it remains stable across sensor reset, hub replacement, macOS
CoreBluetooth UUID changes, and Linux adapter changes. Platform-observed addresses
are stored only as diagnostics. The identifier is visible and spoofable because
contract v2 is unauthenticated.

The BTHome packet ID increments modulo 256 once for every new wake-cycle sample
and remains unchanged throughout that sample's advertisement burst. The durable
deduplication key is the stable sensor identity plus packet ID compared with the
sensor's previous accepted advertisement. Database uniqueness also rejects a
captured packet replayed after restart.

The [contract-v2 replay fixture](fixtures/bthome-v2-sensor-v2.json) covers three
sensors, a duplicate, full and partial samples, malformed data, and an unsupported
object. It drives the same application ingestion interface as Bleak.

### Security

Contract v2 keeps BTHome device-info byte `0x40`: regular interval and unencrypted.
Measurements, stable identifiers, and device presence are observable and not
authenticated. Encryption requires a new contract with a 16-byte BTHome bind-key
provisioning, protected key storage, replacement/recovery, and Home Assistant
interoperability design. Keys must never appear in fixtures or logs.

The selected NPKPHCTH-S probe can also report soil pH and N/P/K. Those values stay
absent until the purchased revision's Modbus mapping is hardware-verified and
suitable BTHome semantics are selected.

## Target BTHome contract version 3

Contract v3 adds store-and-forward identity and acquisition time. It remains a
BTHome v2 advertisement; `3` is the Open Plant Pulse contract version, not a new
BTHome format version. Contract v2 remains the implemented compatibility contract
until the v3 sensor encoder, durable queue, Hub decoder/storage, acknowledgements,
fixtures, and hardware delivery tests are complete.

The logical report schema puts acquisition time in position 2 as follows:

| Position | Field | Required | Meaning |
| --- | --- | --- | --- |
| 1 | Report ID | Yes | Durable, sensor-assigned identity for one immutable sample |
| 2 | Observed at | When the sensor clock is known | UTC acquisition datetime |
| 3 | Soil temperature | With the soil source group | RS485 soil probe value |
| 4 | Air relative humidity | With the air source group | SHT45 value |
| 5 | Soil moisture | With the soil source group | RS485 soil probe value |
| 6 | Air temperature | With the air source group | SHT45 value |
| 7 | Conductivity | With the soil source group | RS485 soil probe value |

Logical position is not byte order. BTHome requires object IDs in ascending numeric
order, so the standard timestamp object cannot physically be the second object.
The v3 wire order is:

| Wire order | Object | ID | Encoding | Source |
| --- | --- | --- | --- | --- |
| 1 | Soil temperature | `0x02` | signed 16-bit little-endian; factor 0.01 degrees C | RS485 soil probe |
| 2 | Air relative humidity | `0x2E` | unsigned 8-bit; 1% | SHT45 |
| 3 | Soil moisture | `0x2F` | unsigned 8-bit; 1% | RS485 soil probe |
| 4 | Report ID (`count`) | `0x3E` | unsigned 32-bit little-endian | Durable sensor queue |
| 5 | Air temperature | `0x45` | signed 16-bit little-endian; factor 0.1 degrees C | SHT45 |
| 6 | Acquisition timestamp | `0x50` | unsigned 32-bit little-endian Unix seconds | Sensor clock |
| 7 | Conductivity | `0x56` | unsigned 16-bit little-endian; 1 microS/cm | RS485 soil probe |

The service-data device-info byte remains `0x40`. Report ID `0` is reserved; IDs
increase monotonically and never wrap or repeat within one enrollment. The next ID
and its immutable report are committed together before advertising. A normal reset,
deep sleep, or firmware upgrade must not reset the counter. A destructive reset that
loses the counter invalidates the enrollment and requires an explicit Hub
re-enrollment before reports are accepted. Contract v3 omits BTHome packet ID
`0x00`; the report ID provides durable Hub deduplication and changes whenever the
measurement data changes.

Objects `0x2E` and `0x2F` replace the v2 hundredth-percent objects so a complete v3
report fits legacy BLE advertising. Percentages are rounded to the nearest whole
percentage point and remain constrained to `0` through `100`. With all source
objects and a timestamp, the data after UUID `0xFCD2` is 24 bytes. Together with
the service-data header and the recommended Flags element, this consumes the full
31-byte legacy advertising payload. The complete local name remains in the scan
response. The v2 button event is not emitted in v3 telemetry; forced reports use
the same durable delivery and acknowledgement path as scheduled reports.

Soil and air source-group completeness rules remain unchanged. Report ID is always
present. Timestamp is the only optional report metadata field.

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

Connected-BLE report acknowledgement version 2 is a five-byte token on the existing
read/write characteristic:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Acknowledgement protocol version, `2` |
| 1 | 4 | Report ID, unsigned little-endian |

Only an exact acknowledgement for a durably stored report removes that report from
the sensor queue. A duplicate, delayed, mismatched, or out-of-order token must not
remove another report. Sensor deletion and queue-head advancement must be durable
before the next report is advertised.

## Change policy

- Preserve fixtures for every released BTHome contract version.
- Update sensor encoding, hub decoding, replay behavior, and compatibility tests
  together.
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

## Connected-BLE forced-report acknowledgement version 1

The always-awake diagnostics firmware can request an immediate report from its
Maintenance page, bypassing the current reporting interval without changing the
next scheduled deadline. The forced BTHome packet includes the standard button
event object `0x3A` with press event `0x01`. The hub first durably ingests that
packet, then connects to the existing service and uses its established read/write
characteristic `7f510002-1b15-4c28-9a4a-8d0f4f505000` to acknowledge the exact
request. Reusing the established characteristic avoids stale GATT service caches
during firmware upgrades; payload version and length distinguish acknowledgments
from device configuration.

The characteristic value is a canonical six-byte token:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Protocol version, currently `1` |
| 1 | 4 | Non-zero force-report request ID, unsigned little-endian |
| 5 | 1 | BTHome packet ID |

While a request awaits acknowledgment, firmware returns the token instead of device
configuration when the characteristic is read. The hub verifies that
its packet ID matches the durably ingested advertisement, and writes the identical
token back. Only that exact write changes sensor state to acknowledged; subsequent
reads return device configuration normally. A missing,
mismatched, or late acknowledgment is not reported as success. Repeated callbacks
for one advertisement burst do not trigger repeated Hub connections.

This acknowledgment is deliberately limited to user-initiated reports in the
always-awake diagnostics runtime. Scheduled and production deep-sleep telemetry
remain one-way and never extend their bounded awake window waiting for a Hub.

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

BLE is the only transport. Deterministic tests replay the contract-v2 fixture
above; there is no side channel a sensor may use instead.
