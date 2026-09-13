#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import socket
import time
from typing import Any, Dict
from urllib.request import urlopen


SCHEMA = "open-plant-pulse.simulation.v1"
MOISTURE_CYCLE_SECONDS = 45 * 60


def hub_started_at(hub_url: str) -> datetime:
    health_url = f"{hub_url.rstrip('/')}/api/health"
    try:
        with urlopen(health_url, timeout=2.0) as response:
            payload = json.load(response)
        started_at = datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"unable to read hub clock from {health_url}: {error}") from error
    if started_at.tzinfo is None:
        raise RuntimeError(f"hub clock from {health_url} has no timezone")
    return started_at.astimezone(timezone.utc)


def build_reading(
    sequence: int,
    elapsed_seconds: float,
    started_at: datetime,
    time_scale: float,
) -> Dict[str, Any]:
    simulated_elapsed = elapsed_seconds * time_scale
    cycle = simulated_elapsed / MOISTURE_CYCLE_SECONDS
    moisture = 52.0 - (cycle % 1.0) * 28.0
    daylight = math.sin(simulated_elapsed / (30 * 60))
    observed_at = started_at + timedelta(seconds=simulated_elapsed)
    return {
        "schema": SCHEMA,
        "reading": {
            "sensor_id": "simulated-plant-01",
            "sequence": sequence,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "soil_temperature_c": round(21.4 + daylight * 0.7, 2),
            "moisture_percent": round(moisture, 2),
            "conductivity_us_cm": round(980 + (52.0 - moisture) * 12),
            "air_temperature_c": round(23.8 + daylight * 2.2, 2),
            "air_humidity_percent": round(54.0 - daylight * 8.0, 2),
            "soil_ph": round(5.3 + math.sin(simulated_elapsed / (90 * 60)) * 0.08, 2),
            "nitrogen_mg_kg": 86,
            "phosphorus_mg_kg": 41,
            "potassium_mg_kg": 124,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Broadcast simulated Open Plant Pulse readings")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--hub-url", default="http://127.0.0.1:8080")
    parser.add_argument("--interval", default=2.0, type=float)
    parser.add_argument(
        "--time-scale",
        default=60.0,
        type=float,
        help="simulated seconds per real second (default: 60)",
    )
    args = parser.parse_args()
    if args.interval <= 0 or args.time_scale <= 0:
        parser.error("--interval and --time-scale must be positive")

    simulation_started_at = hub_started_at(args.hub_url)
    initial_elapsed_seconds = max(
        0.0,
        (datetime.now(timezone.utc) - simulation_started_at).total_seconds(),
    )
    started_at = time.monotonic()
    sequence = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        print(f"Simulated sensor broadcasting to udp://{args.host}:{args.port}")
        try:
            while True:
                envelope = build_reading(
                    sequence,
                    initial_elapsed_seconds + time.monotonic() - started_at,
                    simulation_started_at,
                    args.time_scale,
                )
                sender.sendto(json.dumps(envelope).encode("utf-8"), (args.host, args.port))
                reading = envelope["reading"]
                print(
                    f"sample {sequence}: moisture={reading['moisture_percent']}% "
                    f"air={reading['air_temperature_c']} C "
                    f"simulated_at={reading['observed_at']}"
                )
                sequence += 1
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()