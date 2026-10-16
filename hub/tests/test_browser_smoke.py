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
from open_plant_pulse_hub.application.ingestion import AdvertisementIngestionService
from open_plant_pulse_hub.ingestion.replay import AdvertisementReplay
from open_plant_pulse_hub.web import create_server, server_address

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PNG = bytes.fromhex("89504e470d0a1a0a") + b"a png, as far as anyone here cares"


FIXTURE_PATH = (
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"
)

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
        # A sensor on the plant, because the sensor panel lives on the
        # plant's configuration page and a plant without one never draws it.
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        # The sensor keeps the plant enrolling gave it, so the household has
        # one of each: a plant somebody weighs by hand and a plant with a
        # probe in it. The pages differ between the two, and both are drawn.
        self.sensor_id = self.store.sensors()[0]["sensor_id"]
        self.store.manage_sensor(self.sensor_id, "White bird", "Living room",
                                 "strelitzia", 32.0, 1800, 1800)
        self.sensor_plant_id = next(
            plant["plant_id"] for plant in self.store.plants()
            if plant["plant_id"] != self.plant_id
        )
        self.store.set_hub_wifi_network("Household")

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

    def test_a_page_shows_itself_before_its_data_arrives(self) -> None:
        """Headings, buttons and boxes do not depend on a round trip."""
        tools = self.devtools()
        tools.call("Network.enable")
        # Slow enough that waiting for data would be plain to see.
        tools.call("Network.emulateNetworkConditions", offline=False, latency=400,
                   downloadThroughput=-1, uploadThroughput=-1)
        tools.call("Page.navigate", url=f"{self.base_url}/plants/{self.plant_id}")
        time.sleep(0.9)
        early = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({
              sections: [...document.querySelectorAll("main [data-section]")]
                .filter(s => !s.hasAttribute("hidden")).length,
              checkbox: !!document.getElementById("plant-cover-follows")?.offsetParent,
              record: !!document.getElementById("plant-weight-record")?.offsetParent,
            })""")["result"]["value"])

        self.assertGreater(early["sections"], 0, "the page waited for data to show itself")
        self.assertTrue(early["checkbox"])
        self.assertTrue(early["record"])
        self.assertEqual(tools.complaints, [])

    def test_every_page_runs_without_complaint(self) -> None:
        tools = self.devtools()
        pages = ["/plants", f"/plants/{self.plant_id}", f"/plants/{self.plant_id}/config",
                 f"/sensors/{self.sensor_id}", f"/sensors/{self.sensor_id}/settings",
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

    def test_the_sensor_panel_knows_which_sensor_the_plant_has(self) -> None:
        """The page names the plant; the panel still has to find the sensor.

        Reading it from the address alone left the console switch with no
        sensor on it, so turning the console on called for nobody.
        """
        tools = self.devtools()
        # The sensor's settings are the sensor's page now, not its plant's.
        tools.call("Page.navigate",
                   url=f"{self.base_url}/sensors/{self.sensor_id}/settings")
        time.sleep(3.5)
        panel = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            (() => {
              const toggle = document.getElementById("detail-console-toggle");
              return JSON.stringify({
                sensorId: toggle.dataset.sensorId || null,
                disabled: toggle.disabled,
                identity: document.getElementById("settings-sensor-id").textContent,
              });
            })()""")["result"]["value"])

        self.assertEqual(panel["sensorId"], self.sensor_id)
        self.assertEqual(panel["identity"], self.sensor_id)
        self.assertFalse(panel["disabled"])
        self.assertEqual(tools.complaints, [])

    def test_the_ph_panel_shows_what_the_probe_said_and_what_it_means(self) -> None:
        """The figures, not just the form: a blank panel is a render that threw."""
        tools = self.devtools()
        tools.call("Page.navigate", url=f"{self.base_url}/sensors/{self.sensor_id}/settings")
        time.sleep(3.5)
        drawn = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            (() => {
              const panel = document.getElementById("sensor-ph-panel");
              return JSON.stringify({
                present: !!panel,
                hidden: panel ? panel.hidden : null,
                raw: document.getElementById("ph-raw")?.textContent,
                corrected: document.getElementById("ph-corrected")?.textContent,
                offset: document.getElementById("ph-offset")?.textContent,
              });
            })()""")["result"]["value"])

        self.assertTrue(drawn["present"])
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

    def test_a_keep_interval_being_chosen_survives_the_poll(self) -> None:
        """Choosing from a menu and reaching Save are seconds apart.

        The panel polls while it is open, and refilling it from the device
        put the old interval back before Save could be clicked. Focus was
        the guard, but a menu is the focused element only while the pointer
        stays on it, which is not long enough.
        """
        scale_id = self.store.adopt_scale("http://scale.local", "Pot scale")["sensor_id"]
        tools = self.devtools()
        tools.call("Page.navigate", url=f"{self.base_url}/sensors/{scale_id}/settings")
        time.sleep(3.5)
        # Chosen the way a menu is chosen, then the pointer goes elsewhere.
        tools.call("Runtime.evaluate", expression="""
            const field = document.getElementById("scale-keep-every");
            field.value = "604800";
            field.dispatchEvent(new Event("change", {bubbles: true}));
            document.body.focus();""")
        time.sleep(4)
        held = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({
              value: document.getElementById("scale-keep-every").value,
              focused: document.activeElement.id,
            })""")["result"]["value"])
        # Still the week that was picked, and not because the menu is held.
        self.assertEqual(held["value"], "604800")
        self.assertNotEqual(held["focused"], "scale-keep-every")
        self.assertEqual(tools.complaints, [])

    def test_the_page_holds_still_while_a_form_is_being_filled_in(self) -> None:
        """Nothing is worth repainting under somebody's hands.

        The panel refreshes once a second, and some of what it shows really
        does change every second -- the live weight, and how long ago the
        scale answered. Written while a menu is open, those are repaints
        underneath a picker that a phone can dismiss, for readings nobody is
        waiting on. So the whole panel waits until the form is done with.
        """
        scale_id = self.store.adopt_scale("http://scale.local", "Pot scale")["sensor_id"]
        tools = self.devtools()
        tools.call("Page.navigate", url=f"{self.base_url}/sensors/{scale_id}/settings")
        time.sleep(3.5)
        tools.call("Runtime.evaluate", expression="""
            window.__touched = [];
            document.getElementById("scale-keep-every").focus();
            new MutationObserver((muts) => {
              for (const m of muts) window.__touched.push(m.target.id || m.target.nodeName);
            }).observe(document.body,
                       {childList: true, subtree: true, attributes: true, characterData: true});
        """)
        # Several polls, including the once-a-second "last answered" counter.
        time.sleep(5)
        state = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({touched: window.__touched.slice(0, 10),
                            active: document.activeElement && document.activeElement.id})
            """)["result"]["value"])
        self.assertEqual(state["touched"], [], "the page should not change while a form has focus")
        self.assertEqual(state["active"], "scale-keep-every")
        self.assertEqual(tools.complaints, [])

    def test_a_panel_is_not_shown_and_hidden_again_on_every_poll(self) -> None:
        """Deciding twice costs the page whatever had the focus.

        Every render showed each section whose page matched and then hid the
        ones belonging to another kind of device, so their hidden attribute
        flipped once a second for as long as the page was open. An element
        focused inside a section that goes away loses the focus and does not
        get it back, which is no way to fill in a form.
        """
        scale_id = self.store.adopt_scale("http://scale.local", "Pot scale")["sensor_id"]
        tools = self.devtools()
        tools.call("Page.navigate", url=f"{self.base_url}/sensors/{scale_id}/settings")
        time.sleep(3.5)
        tools.call("Runtime.evaluate", expression="""
            window.__flips = [];
            for (const section of document.querySelectorAll("[data-sensor-kind]")) {
              new MutationObserver(() => window.__flips.push(section.id))
                .observe(section, {attributes: true, attributeFilter: ["hidden"]});
            }""")
        # Several polls' worth: the page refreshes about once a second.
        time.sleep(5)
        flips = json.loads(tools.call("Runtime.evaluate", returnByValue=True,
                                      expression="JSON.stringify(window.__flips)")["result"]["value"])
        self.assertEqual(flips, [], "no section should be shown only to be hidden again")
        # And the one that belongs to a scale is the one on show.
        shown = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify(Array.from(document.querySelectorAll("[data-sensor-kind]"))
              .filter((s) => !s.hasAttribute("hidden")).map((s) => s.dataset.sensorKind))
            """)["result"]["value"])
        self.assertEqual(set(shown), {"scale"})
        self.assertEqual(tools.complaints, [])

    def test_the_household_page_draws_a_tile_for_the_plant(self) -> None:
        tools = self.devtools()
        tools.call("Page.navigate", url=self.base_url + "/plants")
        time.sleep(3.5)
        drawn = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({
              cards: document.querySelectorAll("#fleet-grid .fleet-card").length,
              covers: document.querySelectorAll("#fleet-grid img.fleet-card-cover").length,
              names: [...document.querySelectorAll("#fleet-grid .fleet-card strong")]
                .map((name) => name.textContent),
              every_cared: [...document.querySelectorAll("#fleet-grid .fleet-card span")]
                .map((span) => span.textContent).join(" | "),
              cared: [...document.querySelectorAll("#fleet-grid .fleet-card")]
                .filter((card) => card.textContent.includes("Spider"))
                .map((card) => card.querySelector("span")?.textContent || "")[0] || "",
            })""")["result"]["value"])

        # One plant with a probe and one weighed by hand; both get a tile,
        # and both say when they were last cared for rather than one of them
        # reporting on its instrument instead.
        self.assertEqual(drawn["cards"], 2)
        self.assertNotIn("Last report", drawn["every_cared"])
        self.assertEqual(drawn["covers"], 1)
        self.assertIn("Spider", drawn["names"])
        self.assertIn("White bird", drawn["names"])
        # Watered, fed and weighed are different facts. The card marks each
        # with a drawing and keeps the word for anyone listening to it.
        self.assertIn("Watered", drawn["cared"])
        self.assertIn("Weighed", drawn["cared"])
        self.assertEqual(tools.complaints, [])


if __name__ == "__main__":
    unittest.main()
