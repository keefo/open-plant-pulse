import logging
import socket
from threading import Event, Thread
from typing import Tuple

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.ingestion.simulation import decode_simulation_datagram


LOGGER = logging.getLogger(__name__)


class SimulationUdpReceiver:
    def __init__(self, store: ReadingStore, host: str = "127.0.0.1", port: int = 8765) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("simulation receiver must bind to a loopback address")
        self._store = store
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((host, port))
        self._socket.settimeout(0.25)
        self._stop = Event()
        self._thread = Thread(target=self._run, name="simulation-udp", daemon=True)

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self._socket.getsockname()
        return str(host), int(port)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._socket.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                datagram, _address = self._socket.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                self._store.add(decode_simulation_datagram(datagram))
            except ValueError as error:
                LOGGER.warning("ignored invalid simulation datagram: %s", error)