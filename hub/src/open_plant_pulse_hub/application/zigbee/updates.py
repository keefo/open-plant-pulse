"""Updating a Zigbee device that normally has its Wi-Fi switched off.

The scale's web server exists for bringing a board up and for flashing it.
In normal running it is off, because a second radio associated to a router
all day is the largest single draw on a battery device. That makes an
update a sequence rather than an upload:

    w 1  ->  wait for an address  ->  POST the image  ->  w 0  ->  confirm

Done by hand this is easy to get half right, and the expensive half is the
last step: a scale left with Wi-Fi on looks perfectly healthy and quietly
costs several times the power. So the hub does the whole sequence, and the
last thing it does is turn the radio back off.

Confirmation is over Zigbee, not over Wi-Fi. Every report carries the
running version, so the hub can see the new firmware with the second radio
already shut down -- which is the only order that leaves the device as it
should be whether or not the update worked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import urllib.error
import urllib.request

LOG = logging.getLogger(__name__)

# Each wait is bounded by what it is actually waiting for.
WIFI_UP_SECONDS = 120.0        # association, DHCP, and mDNS settling
UPLOAD_SECONDS = 180.0         # about a megabyte over 2.4 GHz
REBOOT_SECONDS = 120.0         # the device restarts into the new image
CONFIRM_SECONDS = 120.0        # a report carrying the new version
POLL_SECONDS = 1.0


@dataclass
class UpdateState:
    """Where an update has got to, in terms somebody can read."""

    eui: str
    version: str
    digest: str
    step: str = "starting"
    message: str = "Starting"
    done: bool = False
    ok: bool = False
    started_at: str = ""
    finished_at: Optional[str] = None
    steps: List[Dict[str, str]] = field(default_factory=list)

    def payload(self) -> Dict[str, Any]:
        return {
            "eui": self.eui,
            "version": self.version,
            "digest": self.digest,
            "step": self.step,
            "message": self.message,
            "done": self.done,
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": list(self.steps),
        }


class FirmwareUpdater:
    """Runs the turn-it-on, flash, turn-it-off sequence for one device."""

    def __init__(self, service: Any, firmware: Any) -> None:
        self._service = service
        self._firmware = firmware
        self._lock = threading.Lock()
        self._states: Dict[str, UpdateState] = {}
        self._running: Dict[str, threading.Thread] = {}

    def state(self, eui: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._states.get(eui)
            return state.payload() if state else None

    def start(self, eui: str, digest: str) -> Dict[str, Any]:
        """Begin an update, or say why it cannot begin."""
        image = self._firmware.image(digest)
        if image is None:
            raise ValueError("no such firmware image")
        # A C3 image on a C6 bricks the board until somebody reaches it with
        # a cable, which for a sealed pot on a shelf is the worst outcome
        # this hub can cause. The image says which chip it was built for.
        if image.get("chip") not in (None, "esp32c6"):
            raise ValueError(
                f"that image is for an {image.get('chip')}, and this is an esp32c6")
        data = self._firmware.read(digest)
        if not data:
            raise ValueError("that firmware image is no longer on disk")

        with self._lock:
            running = self._running.get(eui)
            if running and running.is_alive():
                raise ValueError("an update is already running for this device")
            state = UpdateState(
                eui=eui,
                version=str(image.get("version", "")),
                digest=digest,
                started_at=_now(),
            )
            self._states[eui] = state
            thread = threading.Thread(
                target=self._run, args=(state, data), daemon=True,
                name=f"update-{eui}")
            self._running[eui] = thread
            thread.start()
            return state.payload()

    # ---- the sequence ----

    def _run(self, state: UpdateState, data: bytes) -> None:
        try:
            self._step(state, "wifi-on", "Turning the device's Wi-Fi on")
            self._service.command(state.eui, b"w 1")
            host = self._wait_for_wifi(state)

            self._step(state, "uploading",
                       f"Sending {len(data) // 1024} KB to {host}")
            self._upload(host, data)

            self._step(state, "restarting", "Waiting for it to restart")
            self._wait_for_reboot(host)

            # Wi-Fi off BEFORE confirming, so a device whose update failed is
            # still left the way it should sit rather than burning its
            # battery waiting for somebody to notice.
            self._step(state, "wifi-off", "Turning Wi-Fi back off")
            self._service.command(state.eui, b"w 0")

            self._step(state, "confirming", "Waiting for it to report its version")
            running = self._wait_for_version(state)
            if running != state.version:
                raise RuntimeError(
                    f"it restarted on {running or 'an unknown version'},"
                    f" not {state.version}")
            self._finish(state, True, f"Updated to {state.version}, Wi-Fi off")
        except Exception as error:   # noqa: BLE001 - reported, not swallowed
            LOG.exception("update of %s failed", state.eui)
            self._leave_radio_off(state)
            self._finish(state, False, str(error) or error.__class__.__name__)

    def _leave_radio_off(self, state: UpdateState) -> None:
        """Whatever went wrong, do not leave the Wi-Fi on behind us."""
        try:
            self._service.command(state.eui, b"w 0")
        except Exception:
            LOG.exception("could not switch %s back to Zigbee only", state.eui)

    def _wait_for_wifi(self, state: UpdateState) -> str:
        """Wait until the device says its Wi-Fi is up, and says where."""
        # The name is asked for first and separately, because the two
        # can fail for different reasons and reporting the wrong one
        # sends somebody looking at the radio when the hub simply did
        # not know where to send the image.
        host = self._service.host_of(state.eui)
        deadline = time.time() + WIFI_UP_SECONDS
        while time.time() < deadline:
            device = self._device(state.eui)
            last = (device or {}).get("last") or {}
            host = host or self._service.host_of(state.eui)
            if last.get("wifi") is True and host:
                return str(host)
            time.sleep(POLL_SECONDS)
        if not host:
            raise RuntimeError(
                "the hub does not know what this device answers to yet;"
                " it announces that over Zigbee when it joins")
        raise TimeoutError("the device did not bring its Wi-Fi up in time")

    def _upload(self, host: str, data: bytes) -> None:
        request = urllib.request.Request(
            f"http://{host}.local/api/ota", data=data, method="POST",
            headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_SECONDS) as answer:
                body = answer.read().decode(errors="replace").strip()
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"the device refused the image: {error.read().decode(errors='replace')[:120]}"
            ) from error
        except OSError as error:
            raise RuntimeError(f"could not reach {host}.local: {error}") from error
        if body != "ok":
            raise RuntimeError(f"the device answered {body[:120]!r}")

    def _wait_for_reboot(self, host: str) -> None:
        """Gone, then back. Only the second half is worth waiting for."""
        deadline = time.time() + REBOOT_SECONDS
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://{host}.local/api/status", timeout=5
                ):
                    return
            except Exception:
                time.sleep(POLL_SECONDS)
        # Not fatal: the device may already have been told to drop Wi-Fi, or
        # mDNS may be slow to catch up. The version check is what decides.
        LOG.info("%s did not answer over Wi-Fi after restarting", host)

    def _wait_for_version(self, state: UpdateState) -> Optional[str]:
        """The version from a report that arrived after the update began.

        Turning Wi-Fi off is itself a command, and the firmware answers a
        command with an immediate report, so this does not wait for the
        device's next scheduled reading -- which on an hourly scale would
        be an hour.
        """
        deadline = time.time() + CONFIRM_SECONDS
        while time.time() < deadline:
            device = self._device(state.eui)
            last = (device or {}).get("last") or {}
            reported = last.get("fw")
            if reported:
                return str(reported)
            time.sleep(POLL_SECONDS)
        return None

    def _device(self, eui: str) -> Optional[Dict[str, Any]]:
        try:
            return next((d for d in self._service.devices() if d.get("eui") == eui), None)
        except Exception:
            LOG.exception("could not read the device list")
            return None

    # ---- progress ----

    def _step(self, state: UpdateState, step: str, message: str) -> None:
        with self._lock:
            state.step = step
            state.message = message
            state.steps.append({"step": step, "message": message, "at": _now()})

    def _finish(self, state: UpdateState, ok: bool, message: str) -> None:
        with self._lock:
            state.step = "done" if ok else "failed"
            state.message = message
            state.ok = ok
            state.done = True
            state.finished_at = _now()
            state.steps.append({"step": state.step, "message": message, "at": _now()})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
