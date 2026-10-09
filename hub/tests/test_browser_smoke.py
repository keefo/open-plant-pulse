"""Does every page actually run?

The rest of the suite reads app.js as text, which cannot tell a working page
from one whose render function throws on the first line. Two bugs shipped
that way: a file list emptied before it was read, and a date used above the
line that declared it. Both left a section silently blank, both were
invisible to a string assertion, and both would have been caught by loading
the page once.

So this loads the pages in headless Chrome, with data in them, and fails on
any uncaught exception or failed request. It is skipped where Chrome is not
installed, so it costs nothing on a machine that cannot run it.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
from threading import Thread
import time
import unittest
import urllib.request

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.application.photos import PhotoLibrary
from open_plant_pulse_hub.web import create_server, server_address

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PNG = bytes.fromhex("89504e470d0a1a0a") + b"a png, as far as anyone here cares"


class DevTools:
    """Just enough of the protocol to open a page and hear it complain."""

    def __init__(self, url: str) -> None:
        _, _, rest = url.partition("://")
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self.sock.recv(4096)
        self.buffer = self.buffer.split(b"\r\n\r\n", 1)[1]
        self.identifier = 0
        self.complaints: list = []

    def _read(self, count: int) -> bytes:
        while len(self.buffer) < count:
            self.buffer += self.sock.recv(1 << 16)
        taken, self.buffer = self.buffer[:count], self.buffer[count:]
        return taken

    def _receive(self) -> str:
        message = b""
        while True:
            first, second = self._read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            message += self._read(length)
            if first & 0x80:
                return message.decode()

    def call(self, method: str, **params):
        self.identifier += 1
        data = json.dumps(
            {"id": self.identifier, "method": method, "params": params}
        ).encode()
        mask = os.urandom(4)
        frame = bytearray([0x81])
        if len(data) < 126:
            frame.append(0x80 | len(data))
        else:
            frame.append(0x80 | 126)
            frame += struct.pack(">H", len(data))
        frame += mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(frame)
        while True:
            message = json.loads(self._receive())
            if message.get("method") == "Runtime.exceptionThrown":
                details = message["params"]["exceptionDetails"]
                self.complaints.append(
                    details.get("exception", {}).get("description") or details.get("text")
                )
            elif message.get("method") == "Log.entryAdded":
                entry = message["params"]["entry"]
                # Chrome asks every site for a favicon; this one has never
                # had one, and that is not a page failing to run.
                if entry.get("level") == "error" and "favicon" not in (entry.get("url") or ""):
                    self.complaints.append(f"{entry.get('url')}: {entry.get('text')}")
            if message.get("id") == self.identifier:
                return message.get("result", {})


@unittest.skipUnless(CHROME.exists(), "Chrome is not installed here")
class BrowserSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        self.directory = tempfile.TemporaryDirectory()
        library = PhotoLibrary(self.store, Path(self.directory.name))
        # A plant with everything a page draws, so a render that throws on a
        # populated section is not missed by testing an empty one.
        self.plant_id = self.store.create_plant("Spider")["plant_id"]
        self.store.update_plant(self.plant_id, "Spider", profile_id="spider_plant",
                                variety="vittatum")
        self.store.record_plant_weight(self.plant_id, 2_400, "watered")
        self.store.record_plant_weight(self.plant_id, 2_100)
        self.store.record_plant_weight(self.plant_id, 1_800, "thirsty", observed=False)
        self.store.record_plant_care(self.plant_id, "note", note="Looking well")
        library.add(self.plant_id, PNG, taken_at="2019-07-14T17:32:05Z")

        self.server = create_server(self.store, "127.0.0.1", 0, photos=library)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"

        self.profile = tempfile.mkdtemp(prefix="smoke-")
        self.chrome = subprocess.Popen(
            [str(CHROME), "--headless", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={self.profile}", "--remote-debugging-port=0", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def tearDown(self) -> None:
        self.chrome.terminate()
        self.chrome.wait(timeout=20)
        shutil.rmtree(self.profile, ignore_errors=True)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)
        self.store.close()
        self.directory.cleanup()

    def devtools(self) -> DevTools:
        port_file = Path(self.profile) / "DevToolsActivePort"
        deadline = time.time() + 30
        while time.time() < deadline and not port_file.exists():
            time.sleep(0.1)
        self.assertTrue(port_file.exists(), "Chrome did not start")
        port = port_file.read_text().splitlines()[0]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=10
        ) as answer:
            target = next(t for t in json.load(answer) if t["type"] == "page")
        tools = DevTools(target["webSocketDebuggerUrl"])
        self.addCleanup(tools.sock.close)
        tools.call("Page.enable")
        tools.call("Runtime.enable")
        tools.call("Log.enable")
        return tools

    def test_every_page_runs_without_complaint(self) -> None:
        tools = self.devtools()
        pages = ["/plants", f"/plants/{self.plant_id}", f"/plants/{self.plant_id}/config",
                 "/settings"]
        for page in pages:
            tools.call("Page.navigate", url=self.base_url + page)
            time.sleep(2.5)
        self.assertEqual(tools.complaints, [], "the pages reported errors")

    def test_a_plant_page_draws_what_it_was_given(self) -> None:
        tools = self.devtools()
        tools.call("Page.navigate", url=f"{self.base_url}/plants/{self.plant_id}")
        time.sleep(3.5)
        drawn = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({
              photos: document.querySelectorAll(".photo-tile").length,
              caption: document.querySelector(".photo-tile figcaption .photo-date")?.value || "",
              weight: document.getElementById("plant-weight-latest").textContent,
              care: document.querySelectorAll("#plant-care-log .care-entry").length,
              name: document.getElementById("plant-page-name").textContent,
            })""")["result"]["value"])

        # Each of these was blank at some point because a render threw.
        self.assertEqual(drawn["photos"], 1)
        self.assertIn("2019", drawn["caption"])
        self.assertEqual(drawn["weight"], "2.10")
        self.assertEqual(drawn["name"], "Spider")
        self.assertEqual(tools.complaints, [])

    def test_a_date_being_chosen_survives_the_poll(self) -> None:
        """A phone's date sheet does not focus the field it belongs to.

        So the page cannot tell somebody is using it by asking which element
        has focus, and refilling the form every second put the old date back
        while the wheel was still being turned.
        """
        tools = self.devtools()
        tools.call("Page.navigate", url=f"{self.base_url}/plants/{self.plant_id}/config")
        time.sleep(3.5)
        # Changed the way a sheet changes it: a value and an input event,
        # with the field itself not focused.
        tools.call("Runtime.evaluate", expression="""
            const field = document.getElementById("plant-config-acquired");
            field.value = "2021-03-05";
            field.dispatchEvent(new Event("input", {bubbles: true}));
            document.body.focus();""")
        time.sleep(4)
        held = tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({
              value: document.getElementById("plant-config-acquired").value,
              focused: document.activeElement.id,
            })""")["result"]["value"]
        self.assertEqual(json.loads(held)["value"], "2021-03-05")
        self.assertNotEqual(json.loads(held)["focused"], "plant-config-acquired")
        self.assertEqual(tools.complaints, [])

    def test_the_household_page_draws_a_tile_for_the_plant(self) -> None:
        tools = self.devtools()
        tools.call("Page.navigate", url=self.base_url + "/plants")
        time.sleep(3.5)
        drawn = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({
              cards: document.querySelectorAll("#fleet-grid .fleet-card").length,
              covers: document.querySelectorAll("#fleet-grid img.fleet-card-cover").length,
              name: document.querySelector("#fleet-grid .fleet-card strong")?.textContent || "",
              cared: document.querySelector("#fleet-grid .fleet-card span")?.textContent || "",
            })""")["result"]["value"])

        self.assertEqual(drawn["cards"], 1)
        self.assertEqual(drawn["covers"], 1)
        self.assertEqual(drawn["name"], "Spider")
        # Watered and weighed are different facts, and the card says both.
        self.assertIn("Watered", drawn["cared"])
        self.assertIn("weighed", drawn["cared"])
        self.assertEqual(tools.complaints, [])


if __name__ == "__main__":
    unittest.main()
