# Sensors

The firmware for each kind of sensor the hub talks to. One directory per
device, each its own ESP-IDF project, built and flashed on its own.

| Directory | Device | Measures | Reaches the hub by |
| --- | --- | --- | --- |
| [`soil-probe/`](soil-probe/) | XIAO ESP32-C3 | soil moisture, temperature, conductivity, pH and nutrients from an NPKPHCTH-S probe; air from an SHT45 | BLE, as BTHome advertisements the hub listens for |
| [`hx711-scale/`](hx711-scale/) | ESP32-C6 with an HX711 and a bar load cell | what a pot weighs | Wi-Fi, as JSON the hub asks for |

The two answer opposite ways round, and the hub treats them as different
kinds of sensor because of it: a probe announces itself and is heard, a
scale sits at an address and is asked.

## Credentials

No network name or password belongs in a source file: this repository is
public. Each project takes them through `idf.py menuconfig`, which writes
to `sdkconfig` — git ignores that file.

A clean checkout therefore builds firmware with no network configured. That
is the safe failure, but it is a failure: an image built that way joins
nothing and, having no console, can only be recovered over USB. Check the
generated `build/config/sdkconfig.h` before flashing anything that is meant
to reach the network.
