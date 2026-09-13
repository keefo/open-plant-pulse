import argparse
import logging
from pathlib import Path

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.ingestion.udp import SimulationUdpReceiver
from open_plant_pulse_hub.web import create_server, server_address


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Open Plant Pulse development hub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--simulation-host", default="127.0.0.1")
    parser.add_argument("--simulation-port", default=8765, type=int)
    parser.add_argument(
        "--database",
        default=str(Path.home() / ".open-plant-pulse" / "hub.sqlite3"),
        help="SQLite path for persistent readings and care events",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = ReadingStore(database_path=args.database)
    receiver = SimulationUdpReceiver(store, args.simulation_host, args.simulation_port)
    receiver.start()
    server = create_server(store, args.host, args.port)
    host, port = server_address(server)
    print(f"Open Plant Pulse hub listening at http://{host}:{port}")
    print(f"Simulation receiver listening at udp://{receiver.address[0]}:{receiver.address[1]}")

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        receiver.close()
        store.close()


if __name__ == "__main__":
    main()