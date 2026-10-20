#!/usr/bin/env python3
"""Watch the Zigbee link, and send commands over it.

    python3 watch.py            # readings, joins, and every command's outcome
    python3 watch.py --raw      # every line the gateway prints

Type a command and press return. Commands are addressed to a device, so the
first word is its EUI-64, or "*" when only one device is known:

    * t            tare the only device
    * w 0          turn its Wi-Fi off          * w 1   back on
    * i 60         sampling rate               * z 0   Zigbee report floor
    acebe6fffe0d7f18 t                         tare that one specifically
    permit 180     open the network to joining
    list           ask the gateway what it knows

Plain python3 is enough: the serial port is handled by serialport.py using
only the standard library, so there is no need to source the ESP-IDF
environment first.

Only one program can hold a serial port. Stop this before flashing, and
before starting a hub that owns the gateway.
"""
import argparse
import glob
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serialport import Port

# Remembering which port answered turns a search across every candidate into
# a single open, which is the difference between waiting and not noticing.
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "opp-zigbee-port")

devices = {}        # eui -> short address, as the gateway reports them
next_id = [1]


def remember(name):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as f:
            f.write(name)
    except OSError:
        pass


def recall():
    try:
        with open(CACHE) as f:
            name = f.read().strip()
        return name if os.path.exists(name) else None
    except OSError:
        return None


def find_gateway():
    """Ask each candidate port what it is; the gateway answers a list."""
    candidates = glob.glob("/dev/cu.wchusbserial*") + glob.glob("/dev/cu.usbmodem*")
    last = recall()
    if last:
        candidates = [last] + [c for c in candidates if c != last]
    for name in candidates:
        try:
            port = Port(name)
        except Exception:
            continue
        port.write(b'{"t":"list"}\n')
        deadline = time.time() + 1.0
        while time.time() < deadline:
            line = port.readline().decode(errors="replace").strip()
            if line.startswith("{"):
                remember(name)
                return port, name, line
        port.close()
    return None, None, None


def typing_thread(port):
    """Turn what is typed into the JSON the gateway expects."""
    for raw in sys.stdin:
        words = raw.split()
        if not words:
            continue
        if words[0] == "list":
            port.write(b'{"t":"list"}\n')
            continue
        if words[0] == "permit":
            seconds = int(words[1]) if len(words) > 1 else 180
            port.write((json.dumps({"t": "permit", "seconds": seconds}) + "\n").encode())
            continue
        target, rest = words[0], " ".join(words[1:])
        if target == "*":
            if len(devices) != 1:
                print(f"! '*' needs exactly one known device; there are {len(devices)}",
                      flush=True)
                continue
            target = next(iter(devices))
        if not rest:
            print("! a command needs a device and something to say", flush=True)
            continue
        message = {"t": "cmd", "id": next_id[0], "eui": target, "cmd": rest}
        next_id[0] += 1
        port.write((json.dumps(message) + "\n").encode())


def show(message, raw_mode):
    kind = message.get("t")
    stamp = time.strftime("%H:%M:%S")
    if kind == "reading":
        devices[message["eui"]] = message.get("short")
        flags = " ".join(k for k in ("stable", "saturated") if message.get(k))
        print(f"{stamp}  {message.get('grams', 0):8.1f} g  batt {message.get('batt', 0):.2f} V  "
              f"{message['eui']}  lqi {message.get('lqi')} rssi {message.get('rssi')} dBm  {flags}",
              flush=True)
    elif kind == "joined":
        devices[message["eui"]] = message.get("short")
        print(f"{stamp}  .. joined: {message['eui']} as {message.get('short')}", flush=True)
    elif kind == "devices":
        print(f"{stamp}  .. network {message.get('pan')} channel {message.get('channel')}", flush=True)
        for row in message.get("list", []):
            devices[row["eui"]] = row.get("short")
            print(f"{stamp}     {row['eui']}  {row.get('short')}  "
                  f"last heard {row.get('last_seen_s')}s ago", flush=True)
    elif kind == "ack":
        # Sent and delivered are different things: a frame queued for a
        # sleeping device that never collects it fails with status 0xa7.
        ok = message.get("status") == 0
        print(f"{stamp}  {'ok' if ok else '!!'} command {message.get('id')}: "
              f"{message.get('note')} (status 0x{message.get('status', 0):02x})", flush=True)
    elif kind == "permit":
        print(f"{stamp}  .. joining open for {message.get('seconds')}s", flush=True)
    elif raw_mode:
        print(f"{stamp}  {message}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true", help="show every line, not just events")
    ap.add_argument("--port", help="skip the search and use this port")
    args = ap.parse_args()

    if args.port:
        port, name, first = Port(args.port), args.port, None
    else:
        port, name, first = find_gateway()
        if not port:
            sys.exit("No gateway found. Is it plugged in, and is anything else "
                     "holding its port?")
    print(f"# listening on {name}", flush=True)
    print("# commands: '* t' to tare the only device, '* w 0' Wi-Fi off, "
          "'permit 180', 'list'", flush=True)
    threading.Thread(target=typing_thread, args=(port,), daemon=True).start()

    if first:
        try:
            show(json.loads(first), args.raw)
        except ValueError:
            pass
    while True:
        line = port.readline().decode(errors="replace").strip()
        if not line:
            continue
        if not line.startswith("{"):
            if args.raw:
                print(f"   {line}", flush=True)
            continue
        try:
            show(json.loads(line), args.raw)
        except ValueError:
            if args.raw:
                print(f"   {line}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
