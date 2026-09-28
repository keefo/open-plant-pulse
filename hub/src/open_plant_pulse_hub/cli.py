import argparse
import logging
from pathlib import Path
from threading import Thread

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.application.firmware import FirmwareLibrary
from open_plant_pulse_hub.firmware_server import create_firmware_server, firmware_server_address
from open_plant_pulse_hub.ingestion.ble import BleakSubscriber
from open_plant_pulse_hub.ingestion.device_configuration import DeviceConfigurationSynchronizer
from open_plant_pulse_hub.web import create_server, server_address


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Open Plant Pulse development hub")
    # Every address, over IPv4 and IPv6, so http://localhost/ and the
    # computer's .local name both reach it; requests from other computers are
    # refused unless --allow-network is given.
    parser.add_argument("--host", default="::")
    parser.add_argument("--port", default=80, type=int)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="answer other computers too; the interface has no login",
    )
    parser.add_argument("--no-ble", action="store_true", help="disable BTHome BLE collection")
    parser.add_argument(
        "--database",
        default=str(Path.home() / ".open-plant-pulse" / "hub.sqlite3"),
        help="SQLite path for persistent readings and care events",
    )
    parser.add_argument(
        "--firmware-directory",
        default=str(Path.home() / ".open-plant-pulse" / "firmware"),
        help="where uploaded firmware images are kept",
    )
    # A sensor downloads its firmware over the household network, so this one
    # server listens there while the management interface stays on loopback.
    parser.add_argument("--firmware-host", default="0.0.0.0")
    parser.add_argument("--firmware-port", default=8081, type=int)
    parser.add_argument(
        "--no-firmware-server",
        action="store_true",
        help="keep firmware images but do not serve them to sensors",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = ReadingStore(database_path=args.database)
    firmware = FirmwareLibrary(store, args.firmware_directory)

    firmware_server = None
    firmware_port = None
    if not args.no_firmware_server:
        try:
            firmware_server = create_firmware_server(
                firmware, args.firmware_host, args.firmware_port
            )
            firmware_port = firmware_server_address(firmware_server)[1]
            Thread(
                target=firmware_server.serve_forever,
                kwargs={"poll_interval": 0.25},
                name="firmware-http",
                daemon=True,
            ).start()
        except OSError as error:
            # Losing the firmware server costs updates, not readings, so the hub
            # says so and carries on rather than refusing to start.
            logging.getLogger(__name__).warning(
                "firmware server unavailable on %s:%s: %s",
                args.firmware_host,
                args.firmware_port,
                error,
            )

    subscriber = None
    if not args.no_ble:
        subscriber = BleakSubscriber(
            AdvertisementIngestionService(store),
            configuration_synchronizer=DeviceConfigurationSynchronizer(
                store, firmware_port=firmware_port
            ),
        )
        subscriber.start()
    server = create_server(
        store,
        args.host,
        args.port,
        subscriber.health if subscriber else None,
        firmware,
        allow_network=args.allow_network,
    )
    host, port = server_address(server)
    shown_host = "localhost" if host in ("::", "0.0.0.0") else f"[{host}]" if ":" in host else host
    shown_port = "" if port == 80 else f":{port}"
    reach = "any computer on the network" if args.allow_network else "this computer only"
    print(f"Open Plant Pulse hub listening at http://{shown_host}{shown_port} ({reach})")
    print("BTHome BLE collection enabled" if subscriber else "BTHome BLE collection disabled")
    if firmware_server is not None:
        firmware_host, firmware_port = firmware_server_address(firmware_server)
        print(f"Firmware images served at http://{firmware_host}:{firmware_port}/firmware/")
    else:
        print("Firmware images are not being served; sensors cannot update over the air")

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if firmware_server is not None:
            firmware_server.shutdown()
            firmware_server.server_close()
        if subscriber is not None:
            subscriber.close()
        store.close()


if __name__ == "__main__":
    main()
