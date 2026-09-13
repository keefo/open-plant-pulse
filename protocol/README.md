# Sensor-to-hub protocol

This directory owns the contract between the battery sensor and desktop hub.
Neither product may change its payload independently.

Implemented contract version 1 uses unencrypted BTHome v2 service data under
UUID `0xFCD2`:

| Order | Object | ID | Encoding |
| --- | --- | --- | --- |
| 1 | Soil temperature | `0x02` | signed 16-bit little-endian; factor 0.01 degrees C |
| 2 | Moisture | `0x14` | unsigned 16-bit little-endian; factor 0.01% |
| 3 | Conductivity | `0x56` | unsigned 16-bit little-endian; 1 microS/cm |

The first service-data byte is `0x40`: BTHome v2, regular interval, unencrypted.
The [shared fixture](fixtures/bthome-v2.json) records a complete known payload
for decoder and compatibility tests.

The selected NPKPHCTH-S probe can also report soil pH and N/P/K values. Those
measurements are intentionally absent from contract v1 and the proposed SHT45
contract v2 until their Modbus mapping is hardware-verified and suitable BTHome
semantics are selected.

## Planned contract version 2

SHT45 support requires distinct soil and air semantics. The proposed object order
is:

| Order | Object | ID | Source |
| --- | --- | --- | --- |
| 1 | Soil temperature | `0x02` | RS485 soil probe |
| 2 | Air temperature | `0x02` | SHT45 |
| 3 | Air relative humidity | `0x03` | SHT45 |
| 4 | Soil moisture | `0x14` | RS485 soil probe |
| 5 | Conductivity | `0x56` | RS485 soil probe |

Repeated temperature objects receive positional suffixes in BTHome consumers,
so order must remain stable. Before implementation, verify Home Assistant's
entity naming and decide whether positional duplicate temperatures provide a
sufficient user-facing contract. Version 1 remains the implemented and tested
format until matching sensor encoder, hub decoder, and fixtures are committed.

## Change policy

- Add fields in ascending BTHome object-ID order.
- Preserve fixtures for released contract versions.
- Update sensor encoding, hub decoding, and compatibility tests together.
- Treat encryption or identity changes as a new contract version.
- Document receiver behavior before emitting an object Home Assistant may not
  understand.

## Development simulation transport

The runnable simulator sends the full planned reading as a versioned JSON
datagram over localhost UDP. This deliberately resembles one-way advertising but
is not a production protocol and must never be accepted on a non-loopback
interface by default. The
[simulation fixture](fixtures/simulated-reading-v1.json) is the contract for this
development adapter.