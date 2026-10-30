"""A Silicon Labs EZSP dongle behind the layer 2 interface.

The other backend, serial_gateway.py, talks to an ESP32-C6 that runs the
Zigbee stack itself and reports in JSON lines. This one talks to a radio
that runs no stack at all -- a Sonoff Dongle Plus MG24, EmberZNet 7.4.5,
EZSP v13 over ASH at 115200 -- and the stack lives here instead, in this
process, by way of zigpy and bellows.

That is the whole difference, and it is a large one. The C6 did joining,
routing and APS retries on the board; here they happen in Python threads
beside the rest of the hub. What does not change is anything above this
file: both backends answer the same eight-member ZigbeeNetwork, devices
are keyed by EUI-64 either way, and a payload bound for the scale is the
scale's business rather than the gateway's.

Three things in here are not translation but judgement:

* zigpy is asyncio and ZigbeeNetwork is not, so this owns a loop on its
  own thread and hands work across in both directions.
* A frame queued for a sleepy device that never collects it fails with
  APS status 0xa7. zigpy raises DeliveryError rather than handing back a
  status byte, and losing that distinction would put back the bug where
  silence counted as success.
* The scale's cluster is manufacturer-specific, so zigpy has no idea how
  to parse it and should not try. Packets arrive raw and go up as-is.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .network import Delivery, Device, Reading, STATUS_UNKNOWN_DEVICE

LOG = logging.getLogger(__name__)
# The endpoint the hub speaks from. The dongle registers 1, 2 and 242;
# a frame sent from one it does not have is not one devices answer.
COORDINATOR_ENDPOINT = 1

# The profile the scale's frames ride on. Manufacturer-specific clusters
# still need a profile, and the device uses Home Automation.
HOME_AUTOMATION_PROFILE = 0x0104
# What the dongle's own serial port runs at. Fixed rather than probed:
# every EmberZNet NCP build Sonoff ships uses it, and a wrong guess looks
# exactly like a dongle that is not there.
BAUD = 115200
# Delivery statuses the hub invents, kept clear of real APS ones.
STATUS_NO_STACK = 0xFE


def _first_dongle() -> Optional[str]:
    """The port a Silicon Labs dongle appears on, if one is plugged in.

    Deliberately not candidate_ports(): that globs the names an ESP32
    takes, and the two must never pick each other up. A CP210x bridge --
    which is what the Sonoff MG24 uses -- comes up as usbserial or
    SLAB_USBtoUART instead.
    """
    import glob
    found = sorted(glob.glob("/dev/cu.usbserial-*")) + \
        sorted(glob.glob("/dev/cu.SLAB_USBtoUART*"))
    return found[0] if found else None


def _clean(value) -> Optional[str]:
    """A string a device reported, or None where it reported nothing.

    Devices pad these with trailing nulls often enough that the raw value
    is not safe to put on a page.
    """
    if not value:
        return None
    text = str(value).replace("\x00", "").strip()
    return text or None


def _device_type(device) -> Optional[int]:
    """The standard device type from the first real endpoint.

    Endpoint 242 is Green Power, which most mains-powered devices carry
    and none of them are, so it is passed over.
    """
    for number, endpoint in sorted(getattr(device, "endpoints", {}).items()):
        if number in (0, 242):
            continue
        kind = getattr(endpoint, "device_type", None)
        if kind is not None:
            return int(kind)
    return None


def _power_source(device) -> Optional[str]:
    """Mains or battery, from the node descriptor.

    Read here rather than from the Basic cluster because the descriptor
    arrives during the interview and costs no traffic afterwards -- and
    because asking a battery device anything is exactly what a battery
    device is bad at. It answers the question behind most "why is this
    quiet?": a sleeping end device is working as designed.
    """
    descriptor = getattr(device, "node_desc", None)
    if descriptor is None:
        return None
    mains = getattr(descriptor, "is_mains_powered", None)
    if mains is None:
        return None
    return "mains" if mains else "battery"


def _role(device) -> Optional[str]:
    """Whether this device carries other devices' traffic."""
    descriptor = getattr(device, "node_desc", None)
    if descriptor is None:
        return None
    if getattr(descriptor, "is_coordinator", False):
        return "coordinator"
    if getattr(descriptor, "is_router", False):
        return "router"
    if getattr(descriptor, "is_end_device", False):
        return "end device"
    return None


def _firmware(device) -> Optional[str]:
    """The build the device says it is running, if it has said.

    From whatever the interview already cached: the string build id if
    the maker set one, the application version if not. Nothing is asked
    for here -- a getter that talks to the radio is a getter that
    blocks, and the version is not worth waking a sleeping device for.
    """
    for number, endpoint in sorted(getattr(device, "endpoints", {}).items()):
        if number in (0, 242):
            continue
        basic = getattr(endpoint, "basic", None)
        cache = getattr(basic, "_attr_cache", None) if basic else None
        if not cache:
            continue
        build = cache.get(0x4000)          # SWBuildID
        if build:
            return _clean(build)
        version = cache.get(0x0001)        # ApplicationVersion
        if version is not None:
            return str(int(version))
    return None


def _is_mains(device) -> bool:
    """Whether this device is plugged in, whatever it says about dozing."""
    descriptor = getattr(device, "node_desc", None)
    if descriptor is None:
        return False
    return bool(getattr(descriptor, "is_mains_powered", False))


def _is_sleepy(device) -> bool:
    """Whether this device turns its radio off between its own errands.

    A mains router listens all the time and a failure to reach it means
    something. An end device that does not keep its receiver on is
    unreachable by design for most of every minute.
    """
    descriptor = getattr(device, "node_desc", None)
    if descriptor is None:
        return False
    return not getattr(descriptor, "is_receiver_on_when_idle", True)


def _clusters(device) -> tuple:
    """Every input cluster, across every real endpoint.

    What a device can do, which the device type alone does not settle:
    this project's own scale and a SONOFF thermometer both declare type
    0x0302, and only their clusters tell them apart.

    Across all of them and not just the first, because the telling one
    is rarely on endpoint 1. The scale puts the standard clusters there
    and its own weighing cluster on endpoint 11 -- read one endpoint
    deep it looked like a thermometer, which is the exact confusion this
    was written to settle.
    """
    found: set = set()
    for number, endpoint in sorted(getattr(device, "endpoints", {}).items()):
        if number in (0, 242):
            continue
        for cluster in getattr(endpoint, "in_clusters", None) or ():
            found.add(int(cluster))
    return tuple(sorted(found))


def _eui_text(ieee) -> str:
    """zigpy's EUI64 as the lower-case colonless text the hub stores."""
    return str(ieee).replace(":", "").lower()


class EzspGateway:
    """The dongle, behind ZigbeeNetwork.

    Nothing is imported from zigpy until start(): the hub must install and
    run without these packages, because the other backend needs none of
    them and a missing import at module scope would take the whole hub
    down rather than one gateway.
    """

    def __init__(self, port: Optional[str] = None,
                 database: Optional[str] = None) -> None:
        self._port = port or _first_dongle()
        # zigpy keeps its own state: the network key, the PAN, and every
        # device it has met. It is not hub.sqlite3 and must not be tidied
        # away with it -- losing this file makes every device rejoin.
        self._database = database or str(
            Path.home() / ".open-plant-pulse" / "zigbee.sqlite3")
        self._app = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._readings: List[Callable[[Reading], None]] = []
        self._joins: List[Callable[[Device], None]] = []
        self._deliveries: List[Callable[[Delivery], None]] = []
        # Our own request ids. zigpy has a TSN but it wraps at 255 and is
        # the stack's to allocate, so the hub counts its own.
        self._next_request = 1
        self._tsn = 0
        # Devices the hub has asked to report, so it asks once.
        self._configured: set = set()
        # One frame per sleeping device, waiting for it to wake.
        self._waiting: Dict[str, tuple] = {}
        self._permit_until = 0.0
        self._released = False
        self._lock = threading.Lock()

    # --- layer 2 -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="zigbee-ezsp", daemon=True)
        self._thread.start()
        # Long enough to form or resume a network on a healthy dongle, and
        # bounded so a missing one does not hold up the hub's start.
        self._ready.wait(timeout=30)

    def stop(self) -> None:
        """Stop, including when the loop has already stopped itself.

        A gateway that failed to open has a closed loop by the time
        anybody calls this, and asking a closed loop to stop raises --
        which would make a failed start throw again on the way out and
        hide the reason it failed in the first place.
        """
        self._stopping.set()
        loop, app = self._loop, self._app
        if loop is not None and not loop.is_closed():
            if app is not None:
                try:
                    asyncio.run_coroutine_threadsafe(app.shutdown(), loop).result(3)
                except Exception:
                    LOG.debug("Zigbee: the dongle did not shut down cleanly")
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    @property
    def connected(self) -> bool:
        return self._app is not None and not self._stopping.is_set()

    def devices(self) -> List[Device]:
        app = self._app
        if app is None:
            return []
        found: List[Device] = []
        for device in list(app.devices.values()):
            # The coordinator is the dongle itself and is not a device the
            # hub has anything to say about.
            if device.nwk == 0x0000:
                continue
            found.append(Device(
                eui=_eui_text(device.ieee),
                short=int(device.nwk),
                manufacturer=_clean(device.manufacturer),
                model=_clean(device.model),
                device_type=_device_type(device),
                clusters=_clusters(device),
                power_source=_power_source(device),
                role=_role(device),
                firmware=_firmware(device),
            ))
        return found

    def permit_join(self, seconds: int) -> None:
        import time
        self._permit_until = time.monotonic() + max(0, int(seconds))
        self._call(lambda app: app.permit(time_s=seconds))

    def send(self, eui: str, cluster: int, endpoint: int, payload: bytes) -> int:
        with self._lock:
            request_id = self._next_request
            self._next_request += 1
        app = self._app
        if app is None:
            # Answered rather than raised, because every other failure on
            # this path is reported as a delivery and the caller should
            # not have two ways to learn the same thing.
            self._announce_delivery(Delivery(
                request_id, STATUS_NO_STACK, "no Zigbee stack running"))
            return request_id
        asyncio.run_coroutine_threadsafe(
            self._send(request_id, eui, cluster, endpoint, payload), self._loop)
        return request_id

    def on_reading(self, handler: Callable[[Reading], None]) -> None:
        self._readings.append(handler)

    def on_joined(self, handler: Callable[[Device], None]) -> None:
        self._joins.append(handler)

    def on_delivery(self, handler: Callable[[Delivery], None]) -> None:
        self._deliveries.append(handler)

    def remove(self, eui: str) -> None:
        """Asks the device to leave, then drops it.

        Without the leave request a device that is still powered simply
        rejoins and reappears, which reads as a bug rather than a
        decision -- the same reason the other backend sends one.
        """
        import zigpy.types as t

        app = self._app
        if app is None or self._loop is None:
            return
        try:
            ieee = t.EUI64.convert(":".join(
                eui[i:i + 2] for i in range(0, len(eui), 2)))
        except Exception:
            LOG.warning("Zigbee: %s is not an address I can remove", eui)
            return
        asyncio.run_coroutine_threadsafe(app.remove(ieee), self._loop)

    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        self._released = True
        self.stop()

    def reconnect(self) -> None:
        self._released = False
        self.start()

    # --- what the troubleshooting panel asks for -----------------------
    #
    # Beyond the eight members of ZigbeeNetwork, the panel reads a handful
    # of attributes straight off the backend. They are read with getattr
    # and a default, so a backend that lacks one shows a blank rather than
    # failing -- which is forgiving, and also why their absence is easy to
    # miss. The ones that mean something for a dongle are answered here.

    @property
    def pan(self) -> Optional[str]:
        info = self._network_info()
        return f"0x{int(info.pan_id):04x}" if info else None

    @property
    def channel(self) -> Optional[int]:
        info = self._network_info()
        return int(info.channel) if info else None

    @property
    def permit_seconds_left(self) -> int:
        """How much longer joining stays open.

        Counted here rather than asked of the radio: EZSP has no query for
        it, and a number the hub works out from when it opened the window
        is better than none.
        """
        import time
        left = int(self._permit_until - time.monotonic())
        return max(0, left)

    def neighbours(self) -> List[Dict[str, object]]:
        """Who the coordinator can hear directly.

        The C6 reported its neighbour table; zigpy keeps a topology that
        is only populated after a scan it does not run by default. Empty
        is the honest answer until that is wired up, rather than a list
        that looks authoritative and is not.
        """
        return []

    @property
    def uptime_s(self) -> Optional[int]:
        """Nothing to report: a dongle has no life of its own.

        The C6 was a board that could reset while the hub carried on, so
        its uptime and reset reason told you something the hub could not
        otherwise know. This radio is opened and closed by the hub, so its
        uptime is the hub's and saying so twice would be noise.
        """
        return None

    @property
    def reset_reason(self) -> Optional[str]:
        return None

    def _network_info(self):
        app = self._app
        try:
            return app.state.network_info if app is not None else None
        except Exception:
            return None

    # --- the loop this backend owns ------------------------------------
    def _run(self) -> None:
        try:
            import zigpy.config  # noqa: F401  (checked here, used below)
        except ImportError:
            LOG.warning(
                "Zigbee dongle support needs zigpy and bellows; "
                "install them or run with the c6 backend")
            self._ready.set()
            return
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._open())
            loop.run_forever()
        except Exception:
            LOG.exception("Zigbee EZSP gateway stopped")
        finally:
            self._ready.set()
            self._app = None
            loop.close()

    async def _open(self) -> None:
        if self._port is None:
            LOG.warning("No Zigbee dongle found on any serial port")
            self._ready.set()
            return
        import zigpy.config as conf
        from bellows.zigbee.application import ControllerApplication

        settings = {
            conf.CONF_DATABASE: self._database,
            conf.CONF_DEVICE: {
                conf.CONF_DEVICE_PATH: self._port,
                conf.CONF_DEVICE_BAUDRATE: BAUD,
            },
            # Off deliberately. zigpy's over-the-air update machinery
            # fetches manufacturer firmware indexes from the internet,
            # which a local-first hub has no business doing, and its
            # default providers fail their own schema validation in
            # zigpy 2.4.0 besides. The scale's updates go over its own
            # Wi-Fi, through updates.py.
            conf.CONF_OTA: {conf.CONF_OTA_ENABLED: False},
        }
        # auto_form writes a network on first run and resumes it after.
        # Resuming is the common case and the one that must be quiet: a
        # gateway that formed a fresh network on every start would hand
        # out a new key and orphan every device each time.
        self._app = await ControllerApplication.new(
            settings, auto_form=True, start_radio=True)
        self._app.add_listener(self)
        LOG.info("Zigbee dongle ready on %s", self._port)
        self._ready.set()

    def _call(self, work) -> None:
        app, loop = self._app, self._loop
        if app is None or loop is None:
            return
        asyncio.run_coroutine_threadsafe(work(app), loop)

    def _hold(self, eui: str, request_id: int, cluster: int,
              endpoint: int, payload: bytes) -> None:
        """Keeps one unheard frame per device until it next speaks."""
        with self._lock:
            # One, not a queue. Asking twice for the same reading is of
            # no use, and a device that has been asleep for a day should
            # not wake to a morning of stale questions.
            self._waiting[eui] = (request_id, cluster, endpoint, payload)
        LOG.info("zigbee: %s is asleep; holding the request until it speaks", eui)

    def _flush(self, eui: str) -> None:
        """Sends what was held, now that the device is listening."""
        with self._lock:
            held = self._waiting.pop(eui, None)
        if held is None:
            return
        request_id, cluster, endpoint, payload = held
        asyncio.run_coroutine_threadsafe(
            self._send(request_id, eui, cluster, endpoint, payload), self._loop)

    def _next_tsn(self) -> int:
        """1 to 255, wrapping. Only ever has to differ from the last one."""
        with self._lock:
            self._tsn = self._tsn % 255 + 1
            return self._tsn

    async def _send(self, request_id: int, eui: str, cluster: int,
                    endpoint: int, payload: bytes) -> None:
        import zigpy.types as t
        from zigpy.exceptions import DeliveryError

        app = self._app
        device = None
        for candidate in list(app.devices.values()):
            if _eui_text(candidate.ieee) == eui.replace(":", "").lower():
                device = candidate
                break
        if device is None:
            # The same answer the C6 gives for an address it does not know,
            # so the hub above cannot tell the backends apart by their
            # failures either.
            self._announce_delivery(Delivery(
                request_id, STATUS_UNKNOWN_DEVICE, "device not on this network"))
            return

        packet = t.ZigbeePacket(
            src=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0x0000),
            src_ep=COORDINATOR_ENDPOINT,
            dst=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=device.nwk),
            dst_ep=endpoint,
            # A transaction number, which the stack needs to match an
            # acknowledgement to the frame that earned it. Left as None
            # every send failed before it reached the radio, reported as
            # "int() argument must be ... not 'NoneType'".
            tsn=t.uint8_t(self._next_tsn()),
            profile_id=HOME_AUTOMATION_PROFILE,
            cluster_id=cluster,
            data=t.SerializableBytes(bytes(payload)),
            extended_timeout=_is_sleepy(device) and not _is_mains(device),
        )
        try:
            await app.send_packet(packet)
        except DeliveryError as error:
            # A battery device keeps its radio off, and the coordinator
            # holds a frame for a sleeping child for only a few seconds
            # before dropping it. Asking such a device anything between
            # its check-ins therefore always fails -- not because the
            # device is unwell, but because nobody was listening. Kept,
            # and sent again the moment it speaks.
            if _is_sleepy(device):
                self._hold(eui, request_id, cluster, endpoint, payload)
                return
            status = getattr(error, "status", None)
            self._announce_delivery(Delivery(
                request_id,
                int(status) if status is not None else 0xA7,
                str(error)))
            return
        except Exception as error:  # a backend must not take the hub down
            self._announce_delivery(Delivery(request_id, STATUS_NO_STACK, str(error)))
            return
        self._announce_delivery(Delivery(request_id, 0))

    # --- what zigpy tells us -------------------------------------------
    def handle_message(self, device, profile, cluster, src_ep, dst_ep,
                       message) -> None:
        """Every frame the radio hears, raw.

        This is the event zigpy actually gives a listener. It was written
        as packet_received, which is a method on the controller and not a
        listener callback at all, so it was never once called: the hub
        commanded devices happily and heard nothing back from any of them.

        zigpy parses clusters it knows; the scale's is manufacturer
        specific, so what matters here is that the bytes arrive intact.
        Deciding what they mean is the driver's job, one layer up.
        """
        try:
            eui = _eui_text(device.ieee)
            # It is talking, so it is listening. The one reliable moment
            # to set up a battery device that refused while asleep, and
            # to ask it anything that was asked while it was not.
            self._configure_reporting(device)
            self._flush(eui)
            self._announce_reading(Reading(
                eui=eui,
                kind="frame",
                values={"cluster": int(cluster),
                        "endpoint": int(src_ep or 0),
                        "payload": bytes(message)},
                lqi=getattr(device, "lqi", None),
                rssi=getattr(device, "rssi", None),
            ))
        except Exception:
            # One malformed frame must not stop the radio being read.
            LOG.exception("Zigbee: could not read a frame")

    # What the hub asks each cluster to report, and how often: the
    # attribute, the shortest gap, the longest, and the change worth
    # sending early. The long gap is the one that matters -- it is the
    # heartbeat that says a silent sensor is still alive, and without it
    # a device that simply has nothing new to say looks identical to one
    # that has fallen off the network.
    # The attribute to report and the shortest gap between reports, per
    # cluster. The shortest gap is the battery's protection -- it is what
    # stops a swinging reading becoming a burst of frames -- so it is the
    # hub's to set and not an option. How long a device may stay silent
    # and how small a change is worth sending are the parts worth
    # varying, and they come from the settings below.
    REPORTING = {
        0x0006: (0x0000, 1),       # on/off, within a second of a press
        0x0402: (0x0000, 60),      # temperature
        0x0405: (0x0000, 60),      # humidity
        0x0001: (0x0021, 3600),    # battery, which moves slowly
    }
    # What each word means on each cluster, in the units of the wire:
    # hundredths of a degree, hundredths of a per cent, half-per-cents
    # of charge. A boolean has no degrees of change, so it is always 1.
    CHANGE = {
        0x0006: {"fine": 1, "normal": 1, "coarse": 1},
        0x0402: {"fine": 20, "normal": 50, "coarse": 100},
        0x0405: {"fine": 100, "normal": 200, "coarse": 500},
        0x0001: {"fine": 2, "normal": 2, "coarse": 4},
    }
    DEFAULT_MAX_SECONDS = 1800
    DEFAULT_SENSITIVITY = "normal"
    # Battery is asked for rarely whatever the rest is set to: charge
    # moves over weeks, and waking a cell to say so hourly is the one
    # setting that would cost more than it reports.
    SLOW_CLUSTERS = {0x0001: 43200}

    def reporting_for(self, eui: str) -> tuple:
        """What this device has been asked for: (max seconds, sensitivity).

        Replaced by the service, which knows what the user chose. A
        gateway on its own has no opinion beyond the defaults.
        """
        return (self.DEFAULT_MAX_SECONDS, self.DEFAULT_SENSITIVITY)

    def _configure_reporting(self, device, force: bool = False) -> None:
        """Asks a device to tell the hub when things change.

        Nothing was ever bound, so every device decided for itself
        whether to say anything: the plug answered only when asked and
        never mentioned its own button, and the thermometer went quiet
        for half-hours at a time with no way to tell sleep from a fault.

        Binding is addressed to the coordinator, so reports come here
        rather than to whichever device happened to be listening. It is
        done during the interview because a battery sensor is awake then
        and may not be again for half an hour -- and retried when one
        speaks, which is the other moment it is certainly listening.
        """
        eui = _eui_text(device.ieee)
        with self._lock:
            if not force and eui in self._configured:
                return
            self._configured.add(eui)

        max_seconds, sensitivity = self.reporting_for(eui)

        async def configure():
            for endpoint in getattr(device, "endpoints", {}).values():
                for cluster_id, wanted in self.REPORTING.items():
                    cluster = getattr(endpoint, "in_clusters", {}).get(cluster_id)
                    if cluster is None:
                        continue
                    attribute, minimum = wanted
                    maximum = self.SLOW_CLUSTERS.get(cluster_id, max_seconds)
                    change = self.CHANGE[cluster_id].get(sensitivity, 1)
                    try:
                        await cluster.bind()
                        await cluster.configure_reporting(
                            attribute, minimum, maximum, change)
                        LOG.info("zigbee: %s will report cluster 0x%04x",
                                 eui, cluster_id)
                    except Exception as error:
                        # A sleeping device simply does not answer. Letting
                        # it be retried is better than holding the interview
                        # open for something that will come round again.
                        with self._lock:
                            self._configured.discard(eui)
                        LOG.debug("zigbee: %s would not take reporting for "
                                  "0x%04x: %s", eui, cluster_id, error)
                        return

        asyncio.run_coroutine_threadsafe(configure(), self._loop)

    def reconfigure(self, eui: str) -> bool:
        """Asks a device again, with whatever it is now set to.

        Reporting is agreed once, during the interview, so a setting
        changed afterwards means nothing until the device is asked
        again. A mains device answers at once; a sleeping one will not
        hear, and is caught when it next speaks.
        """
        app = self._app
        if app is None:
            return False
        for device in list(app.devices.values()):
            if _eui_text(device.ieee) == eui.replace(":", "").lower():
                with self._lock:
                    self._configured.discard(eui)
                self._configure_reporting(device, force=True)
                return True
        return False

    def device_initialized(self, device) -> None:
        self._announce_join(device)
        self._ask_firmware(device)
        self._configure_reporting(device)

    def _ask_firmware(self, device) -> None:
        """Asks for the version and the charge, while it is certainly awake.

        The interview reads a device's make and model and stops there,
        so the version was a dash on every page. Asked here because a
        device that has just finished joining is listening; asking later
        means waking a battery sensor to satisfy a figure nobody is
        looking at. A refusal is not worth reporting: plenty of devices
        do not keep a build id, and the page says so by showing none.
        """
        for number, endpoint in sorted(getattr(device, "endpoints", {}).items()):
            if number in (0, 242) or getattr(endpoint, "basic", None) is None:
                continue
            power = getattr(endpoint, "in_clusters", {}).get(0x0001)

            async def ask(basic=endpoint.basic, power=power):
                try:
                    await basic.read_attributes([0x0001, 0x4000])
                except Exception:
                    LOG.debug("no build id from %s", _eui_text(device.ieee))
                # The charge, for the same reason: a cell sitting at full
                # never crosses the change threshold, so the only battery
                # report a new device would otherwise make is its twelve
                # hourly heartbeat -- half a day of a page with no
                # battery on a sensor that has one.
                if power is None:
                    return
                try:
                    await power.read_attributes([0x0020, 0x0021])
                except Exception:
                    LOG.debug("no charge from %s", _eui_text(device.ieee))
            asyncio.run_coroutine_threadsafe(ask(), self._loop)
            return

    def device_joined(self, device) -> None:
        self._announce_join(device)

    def _announce_reading(self, reading: Reading) -> None:
        for handler in list(self._readings):
            try:
                handler(reading)
            except Exception:
                LOG.exception("Zigbee: a reading handler failed")

    def _announce_join(self, device) -> None:
        found = Device(eui=_eui_text(device.ieee), short=int(device.nwk))
        for handler in list(self._joins):
            try:
                handler(found)
            except Exception:
                LOG.exception("Zigbee: a join handler failed")

    def _announce_delivery(self, delivery: Delivery) -> None:
        for handler in list(self._deliveries):
            try:
                handler(delivery)
            except Exception:
                LOG.exception("Zigbee: a delivery handler failed")
