"""Read a freshly flashed sensor's pairing code from its boot log.

A sensor's pairing code proves that whoever adds it is holding it, so it must be
readable without the hub and without a network. A sold sensor carries it on a
label. A self-built one has no label, and cannot show it on its own console
either: that console needs Wi-Fi, Wi-Fi credentials arrive over the paired link,
and the link needs the code. The one moment the code is reachable is at the
bench, over USB, which is where this runs.

Resets the board and prints the code. Nothing is transmitted anywhere.
"""

import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover - environment guidance, not logic
    sys.exit("pyserial is required: use the ESP-IDF python, or pip install pyserial")

PATTERN = "Pairing code for this device's label:"
BOOT_SECONDS = 20


def main() -> int:
    if len(sys.argv) < 2:
        return int(bool(sys.exit("usage: read-pairing-code.py /dev/cu.usbmodemXXXX")))
    port_name = sys.argv[1]

    with serial.Serial(port_name, 115200, timeout=1) as port:
        port.setDTR(False)
        port.setRTS(True)
        time.sleep(0.2)
        port.setRTS(False)  # release reset so the board boots

        deadline = time.time() + BOOT_SECONDS
        while time.time() < deadline:
            line = port.readline().decode("utf-8", "replace").strip()
            if PATTERN in line:
                code = line.rsplit(":", 1)[1].strip()
                print()
                print(f"    Pairing code: {code}")
                print()
                print("    Write this on the sensor before closing its case.")
                print("    Your hub asks for it once, when the sensor is added.")
                print()
                return 0

    print(
        "No pairing code seen. The board may not have restarted; unplug it,\n"
        "plug it back in, and run this again.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
