# Simulated sensor

This development tool broadcasts realistic, changing plant readings to the hub
over localhost UDP. It models the complete planned sensor set without claiming
that unresolved NPKPHCTH-S fields are available through production BTHome.

From the repository root:

```sh
python3 simulator/sensor.py
```

The default destination is `udp://127.0.0.1:8765`, with one JSON datagram every
two real seconds. The default `--time-scale 60` makes each real second represent
one simulated minute, so each packet advances plant time by two minutes and the
45-minute moisture cycle completes in 45 real seconds.

Start the hub first. The simulator reads `/api/health` from
`http://127.0.0.1:8080` and uses the web service's `started_at` as simulation time
zero. Use `--hub-url` when the hub listens at another address.

Use `--time-scale 1` for real time or choose a faster scale for focused tests:

```sh
python3 simulator/sensor.py --interval 2 --time-scale 120
```

The simulator advances both sensor values and `observed_at` using the same clock.
The hub uses that observation time for watering cooldowns and pot-response windows,
while retaining the real receipt time separately. `--interval` and `--time-scale`
must both be positive. This is a development transport, not the production radio
protocol. The envelope is documented by the shared fixture under
`protocol/fixtures/`.