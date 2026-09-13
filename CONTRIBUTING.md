# Contributing

## Development workflow

1. Open an issue for hardware changes and include exact part revisions.
2. Keep protocol code independent of ESP-IDF when practical.
3. Add a deterministic host test for parser or payload changes.
4. Run `sh scripts/check.sh`.
5. For target changes, run `idf.py build` and describe hardware validation in the
   pull request.

Do not present candidate wiring, register maps, thresholds, battery estimates,
or ingress ratings as verified facts. Link the primary manual or include measured
evidence. Never commit secrets, Home Assistant exports, BLE encryption keys, or
device-specific credentials.

## Commit scope

Prefer focused commits under `sensor`, `hub`, `protocol`, `deploy`, `docs`, or
`examples`. Changes to the sensor-to-hub payload must update both products and a
shared protocol fixture in the same pull request.
Generated ESP-IDF build output and local `sdkconfig` files are ignored; update
`sensor/sdkconfig.defaults` only for portable project defaults.