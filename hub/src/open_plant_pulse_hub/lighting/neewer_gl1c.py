"""The Neewer GL1C LED panel, over its WiFi protocol on UDP port 5052.

Ported from the glowlight project's gl1cd.py, which ran this light for months.
Facts worth not rediscovering:

* The light expects one long-lived controller holding a subscribed session with
  a keepalive about once a second. Connecting, sending a command and dropping
  the socket leaves it refusing new sessions, so the driver keeps one session
  open for as long as it runs.
* Every message is 80 <type> <len> <payload> <checksum>, the checksum being the
  sum of the preceding bytes masked to 8 bits. The light replies to port 5052,
  not to the sender's port, so the driver binds 5052 itself. Neewer Control
  Center binds it too, exclusively; the two cannot run on one computer.
* It is identified by the MAC inside its discovery beacon, which is its BLE MAC,
  not its WiFi MAC, and located by sweeping the local subnet, so a new DHCP
  address needs no edit.
* It reports power (0x07) but never brightness or colour. The colour frame is
  brightness, CCT, tint, verified against the light on 2026-09-25; a reversed
  guess once ran it at 50% with a heavy tint for a day, and nothing on the wire
  can catch that.
* In standby it drops WiFi and BLE, so after a mains power cut nothing in
  software can wake it; only its own button can.
"""
import logging
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .driver import (
    CONFIRMED,
    UNCONFIRMED,
    UNREACHABLE,
    ConfigField,
    LightDriver,
    LightState,
    PowerResult,
)


LOG = logging.getLogger(__name__)
PORT = 5052
STALE_SECONDS = 10.0
QUICK_PROBE_SECONDS = 3.0
SWEEP_SECONDS = 60.0
CONFIRM_SECONDS = 1.2
GM_NEUTRAL_WIRE = 50

HANDSHAKE, KEEPALIVE, COMMAND, SUBSCRIBE = 0x02, 0x04, 0x05, 0x06
BEACON, HEARTBEAT, POWER_REPORT = 0x01, 0x03, 0x07


def frame(message_type: int, payload: bytes = b"") -> bytes:
    body = bytes([0x80, message_type, len(payload)]) + payload
    return body + bytes([sum(body) & 0xFF])


def handshake_frame(own_ip: str) -> bytes:
    address = own_ip.encode()
    return frame(HANDSHAKE, bytes([0x00, 0x00, len(address)]) + address)


def power_frame(on: bool) -> bytes:
    return frame(COMMAND, bytes([0x01, 1 if on else 0]))


def colour_frame(brightness: int, cct_kelvin: int, tint: int) -> bytes:
    """Brightness %, CCT in kelvin, green-magenta tint -50..+50 (0 = neutral)."""
    return frame(
        COMMAND,
        bytes(
            [
                0x02,
                max(0, min(100, brightness)),
                max(29, min(70, cct_kelvin // 100)),
                max(0, min(100, GM_NEUTRAL_WIRE + tint)),
            ]
        ),
    )


def beacon_mac(packet: bytes) -> Optional[str]:
    """The 6-byte MAC in a GL1C discovery beacon, as aa:bb:cc:dd:ee:ff."""
    if len(packet) < 6 or packet[0] != 0x80 or packet[1] != BEACON or b"GL1C" not in packet:
        return None
    body, index = packet[3:-1], 0
    while index < len(body) - 1:
        length = body[index + 1]
        value = body[index + 2 : index + 2 + length]
        if length == 6 and len(value) == 6:
            return value.hex(":")
        index += 2 + length
    return None


def local_ipv4_and_netmask() -> Tuple[Optional[str], Optional[str]]:
    """This computer's address and mask on the interface with the default route."""
    try:
        route = subprocess.run(
            ["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5
        ).stdout
        device = next(
            (line.split(":")[1].strip() for line in route.splitlines() if "interface:" in line),
            None,
        )
        if device:
            for line in subprocess.run(
                ["ifconfig", device], capture_output=True, text=True, timeout=5
            ).stdout.splitlines():
                parts = line.split()
                if parts and parts[0] == "inet":
                    mask = parts[parts.index("netmask") + 1] if "netmask" in parts else None
                    return parts[1], mask
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        pass
    # Not macOS, or no route tool: the address a datagram towards the internet
    # would leave from. Nothing is sent.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0], None
    except OSError:
        return None, None


def hosts_on_subnet() -> List[str]:
    """Every host on this computer's subnet; anything wider than /22 is cut to 254."""
    own_ip, mask = local_ipv4_and_netmask()
    if not own_ip:
        return []
    if mask is None:
        mask_value = 0xFFFFFF00
    elif mask.startswith("0x"):
        mask_value = int(mask, 16)
    else:
        mask_value = struct.unpack("!I", socket.inet_aton(mask))[0]
    network = struct.unpack("!I", socket.inet_aton(own_ip))[0] & mask_value
    size = (~mask_value) & 0xFFFFFFFF
    if size > 1024:
        size = 255
    return [socket.inet_ntoa(struct.pack("!I", network + i)) for i in range(1, size)]


class NeewerGL1C(LightDriver):
    kind = "neewer-gl1c"
    label = "Neewer GL1C panel"
    capabilities = ("power", "level", "colour")
    config_fields = (
        ConfigField(
            "mac",
            "Beacon MAC",
            "mac",
            help="Leave empty to take the first GL1C found; it is then kept.",
        ),
        ConfigField("brightness", "Brightness (%)", "integer", default=100, minimum=0, maximum=100),
        ConfigField("cct", "Colour temperature (K)", "integer", default=4200, minimum=2900, maximum=7000),
        ConfigField("tint", "Green-magenta tint", "integer", default=0, minimum=-50, maximum=50),
    )
    learned_fields = ("last_ip",)

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._socket: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._stopping = threading.Event()
        self._ip: Optional[str] = None
        self._own_ip: Optional[str] = None
        self._subscribed = False
        self._power: Optional[bool] = None
        self._power_reported = threading.Condition()
        self._last_rx = 0.0
        self._seen: Dict[str, str] = {}
        self._problem: Optional[str] = None
        self._last_quick = 0.0
        self._last_sweep = 0.0
        self._last_send_error = 0.0

    # --- interface ------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._session, name="gl1c-session", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        if self._socket is not None:
            self._socket.close()

    def state(self) -> LightState:
        if self._socket is None:
            return LightState(False, None, self._problem or "starting")
        if not self._online():
            return LightState(False, None, self._problem or "searching the network for the light")
        if not self._subscribed:
            return LightState(True, None, "establishing a session")
        return LightState(True, self._power)

    def set_power(self, on: bool) -> PowerResult:
        with self._command_lock:
            if not self._online() or not self._subscribed:
                return PowerResult(
                    UNREACHABLE,
                    "the light is not on the network; if it lost mains power, "
                    "press its power button",
                )
            with self._power_reported:
                # Cleared first, so only a reply to this command can confirm it.
                self._power = None
                self._send(power_frame(on))
                self._power_reported.wait_for(lambda: self._power is not None, CONFIRM_SECONDS)
                reported = self._power
            if on:
                # The light cannot be asked its colour and may come up at
                # whatever someone last set by hand, so the preset goes every time.
                self._send_colour()
            if reported is None:
                return PowerResult(UNCONFIRMED, "the light did not report its power")
            if reported != on:
                return PowerResult(UNCONFIRMED, f"the light reports {'on' if reported else 'off'}")
            return PowerResult(CONFIRMED)

    # --- session --------------------------------------------------------
    def _online(self) -> bool:
        return bool(self._ip) and time.monotonic() - self._last_rx <= STALE_SECONDS

    def _bind(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", PORT))
            sock.settimeout(0.5)
        except OSError as error:
            sock.close()
            self._problem = (
                f"cannot listen on UDP {PORT} ({error.strerror or error}); "
                "is gl1cd or Neewer Control Center running?"
            )
            return False
        self._socket = sock
        self._problem = None
        threading.Thread(target=self._reader, name="gl1c-reader", daemon=True).start()
        return True

    def _session(self) -> None:
        """Hold one session for as long as the driver runs."""
        while not self._stopping.is_set() and self._socket is None:
            if not self._bind():
                LOG.warning("GL1C: %s", self._problem)
                self._stopping.wait(10)
        if self._socket is None:
            return
        self._ip = self.config.get("last_ip")
        self._relocate(quick=False)
        while not self._stopping.is_set():
            # Nothing here may end the loop: a worker that died on one
            # transient ENETUNREACH once left a controller running with no
            # session, for good.
            try:
                self._tick()
            except Exception:
                LOG.exception("GL1C session tick failed")
            self._stopping.wait(1.0)

    def _tick(self) -> None:
        now = time.monotonic()
        if not self._ip or not self._online():
            if now - self._last_quick >= QUICK_PROBE_SECONDS:
                self._last_quick = now
                if self._relocate(quick=True):
                    return
            if now - self._last_sweep >= SWEEP_SECONDS:
                self._last_sweep = now
                self._relocate(quick=False)
            if not self._ip:
                return
        if not self._subscribed:
            self._own_ip = local_ipv4_and_netmask()[0] or self._own_ip
            if self._own_ip:
                self._send(handshake_frame(self._own_ip))
                self._stopping.wait(0.4)
                self._send(frame(SUBSCRIBE, bytes([0x01])))
                self._stopping.wait(0.4)
        self._send(frame(KEEPALIVE))

    def _relocate(self, quick: bool) -> bool:
        found = self._resolve(quick)
        if found is None:
            return False
        if found != self._ip:
            LOG.info("GL1C located at %s%s", found, f" (was {self._ip})" if self._ip else "")
            self._ip = found
            self.learn(last_ip=found)
        self._subscribed = False
        return True

    def _resolve(self, quick: bool) -> Optional[str]:
        """Find the light by its beacon MAC: the cached address first, then the subnet."""
        groups: List[List[str]] = []
        if self.config.get("last_ip"):
            groups.append([self.config["last_ip"]])
        if not quick:
            groups.append(hosts_on_subnet())
        probe = frame(KEEPALIVE)
        for group in groups:
            if not group:
                continue
            self._seen.clear()
            for host in group:
                self._send(probe, host, quiet=True)
            deadline = time.monotonic() + (1.0 if len(group) == 1 else 6.0)
            while time.monotonic() < deadline and not self._stopping.is_set():
                for ip, mac in list(self._seen.items()):
                    wanted = self.config.get("mac")
                    if wanted is None:
                        LOG.info("GL1C: learned beacon MAC %s at %s", mac, ip)
                        self.learn(mac=mac)
                        return ip
                    if mac == wanted:
                        return ip
                self._stopping.wait(0.2)
        return None

    def _reader(self) -> None:
        sock = self._socket
        while not self._stopping.is_set() and sock is not None:
            try:
                packet, (address, _) = sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                time.sleep(0.2)
                continue
            try:
                self._receive(packet, address)
            except Exception:
                LOG.exception("GL1C: could not handle a packet from %s", address)

    def _receive(self, packet: bytes, address: str) -> None:
        if len(packet) < 2 or packet[0] != 0x80:
            return
        mac = beacon_mac(packet)
        if mac:
            self._seen[address] = mac
        # Only the light's own address counts as liveness; anything else only
        # feeds discovery.
        if address != self._ip:
            return
        self._last_rx = time.monotonic()
        if packet[1] == HEARTBEAT:
            self._subscribed = True
        elif packet[1] == BEACON:
            self._subscribed = False
        elif packet[1] == POWER_REPORT and len(packet) >= 6 and packet[3] == 0x01:
            with self._power_reported:
                self._power = bool(packet[4])
                self._power_reported.notify_all()

    def _send_colour(self) -> None:
        packet = colour_frame(
            int(self.config.get("brightness", 100)),
            int(self.config.get("cct", 4200)),
            int(self.config.get("tint", 0)),
        )
        # UDP drops are silent and the light never reports its colour, so twice.
        self._send(packet)
        time.sleep(0.12)
        self._send(packet)

    def _send(self, packet: bytes, host: Optional[str] = None, quiet: bool = False) -> bool:
        """Never raises."""
        target = host or self._ip
        sock = self._socket
        if not target or sock is None:
            return False
        try:
            with self._send_lock:
                sock.sendto(packet, (target, PORT))
            return True
        except OSError as error:
            now = time.monotonic()
            if not quiet and now - self._last_send_error > 60:
                self._last_send_error = now
                LOG.warning("GL1C: send to %s failed (%s); will keep retrying", target, error)
            return False
