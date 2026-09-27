"""Run a throwaway hub with seeded sensors, for looking at the browser pages.

Not part of the automated checks. It exists so the guided flow can be walked and
reviewed by a person, which is what phase 1 of the onboarding proposal asks for.

    PYTHONPATH=hub/src python3 hub/tests/manual_preview_server.py [port]
"""

from pathlib import Path
import sys
from threading import Thread

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.ingestion.replay import AdvertisementReplay
from open_plant_pulse_hub.web import create_server, server_address

FIXTURE_PATH = Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    store = ReadingStore()
    AdvertisementReplay(AdvertisementIngestionService(store)).replay(FIXTURE_PATH)
    observed = store.sensors("unclaimed")
    if observed:
        store.manage_sensor(
            observed[0]["sensor_id"], "white bird", "living", "monstera", None, None, 1800
        )
        store.set_hub_wifi_network("BEYONDCOW-2.4G")
        store.set_sensor_wifi_enabled(observed[0]["sensor_id"], True)
        store.record_sensor_wifi_result(
            observed[0]["sensor_id"], "joined", None, "192.168.0.111"
        )
    server = create_server(store, "127.0.0.1", port)
    host, bound = server_address(server)
    print(f"preview hub at http://{host}:{bound}/settings")
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            store.wait_for_reading(60.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
