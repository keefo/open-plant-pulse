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
from datetime import datetime, timedelta, timezone
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
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"

    @classmethod
    def setUpClass(cls) -> None:
        """One browser for the whole class.

        Starting Chrome costs about a second and tearing it down costs
        more, which was paid eleven times over for a browser that each test
        then used identically. Tests still get a fresh store, a fresh
        server and a blank page; what they share is the process.
        """
        cls.profile = tempfile.mkdtemp(prefix="smoke-")
        cls.chrome = subprocess.Popen(
            [str(CHROME), "--headless", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={cls.profile}", "--remote-debugging-port=0", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.chrome.terminate()
        cls.chrome.wait(timeout=20)
        shutil.rmtree(cls.profile, ignore_errors=True)

    def tearDown(self) -> None:
        # Back to nothing, so one test's page cannot be the next one's
        # starting state. The browser persists; what it is showing does not.
        try:
            tools = DevTools(self._target_url())
            tools.call("Page.navigate", url="about:blank")
            tools.sock.close()
        except Exception:
            pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)
        self.store.close()
        self.directory.cleanup()

    # Counting the page's own requests, so a test can wait for data to have
    # arrived rather than for a number of seconds to have passed. Injected
    # before any document runs, which is the only way to see the first fetch
    # a page makes.
    WATCH_FETCHES = """
        window.__settled = 0;
        window.__inflight = 0;
        window.__lastSettle = 0;
        const original = window.fetch;
        window.fetch = function (...args) {
            window.__inflight++;
            return original.apply(this, args).finally(() => {
                window.__inflight--;
                window.__settled++;
                window.__lastSettle = Date.now();
            });
        };
    """

    # A fetch resolving is not the page having drawn it: the counter above
    # ticks in a finally, while the code that renders the answer runs after
    # the await that follows. So readiness is the network going quiet --
    # nothing in flight, and nothing new for long enough that the render
    # after the last answer has certainly run.
    QUIET_MS = 150

    def wait_until(self, tools: "DevTools", expression: str,
                   timeout: float = 15.0, note: str = "") -> None:
        """Poll the page until it says yes, or give up and say what it said.

        These were fixed sleeps of three and a half to five seconds each,
        which is both slower than the page (it polls every second) and less
        certain than asking it: a slow machine could still read the page
        before it was ready, and a fast one waited anyway.
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            answer = tools.call("Runtime.evaluate", returnByValue=True,
                                expression=f"Boolean({expression})")
            last = answer.get("result", {}).get("value")
            if last:
                return
            time.sleep(0.05)
        self.fail(f"waited {timeout}s for {note or expression} (last saw {last!r})")

    def open_page(self, tools: "DevTools", url: str) -> None:
        """Go to a page and wait until its first data has been drawn."""
        tools.call("Page.navigate", url=url)
        self.wait_until(
            tools,
            "document.readyState === 'complete' && window.__settled > 0"
            f" && window.__inflight === 0"
            f" && Date.now() - window.__lastSettle > {self.QUIET_MS}",
            note=f"{url} to load and draw its data",
        )

    def after_a_poll(self, tools: "DevTools", rounds: int = 1) -> None:
        """Wait for the page to poll again, by watching it actually happen."""
        before = tools.call("Runtime.evaluate", returnByValue=True,
                            expression="window.__settled")["result"]["value"]
        self.wait_until(
            tools,
            f"window.__settled >= {before} + {rounds} && window.__inflight === 0"
            f" && Date.now() - window.__lastSettle > {self.QUIET_MS}",
            note=f"{rounds} more poll(s) to complete and be drawn")

    def _target_url(self) -> str:
        port_file = Path(self.profile) / "DevToolsActivePort"
        deadline = time.time() + 30
        while time.time() < deadline and not port_file.exists():
            time.sleep(0.05)
        self.assertTrue(port_file.exists(), "Chrome did not start")
        port = port_file.read_text().splitlines()[0]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=10
        ) as answer:
            target = next(t for t in json.load(answer) if t["type"] == "page")
        return target["webSocketDebuggerUrl"]

    def devtools(self) -> DevTools:
        tools = DevTools(self._target_url())
        self.addCleanup(tools.sock.close)
        tools.call("Page.enable")
        tools.call("Runtime.enable")
        tools.call("Log.enable")
        tools.call("Page.addScriptToEvaluateOnNewDocument", source=self.WATCH_FETCHES)
        return tools

    def test_a_page_shows_itself_before_its_data_arrives(self) -> None:
        """Headings, buttons and boxes do not depend on a round trip."""
        tools = self.devtools()
        tools.call("Network.enable")
        # Slow enough that waiting for data would be plain to see.
        tools.call("Network.emulateNetworkConditions", offline=False, latency=400,
                   downloadThroughput=-1, uploadThroughput=-1)
        tools.call("Page.navigate", url=f"{self.base_url}/plants/{self.plant_id}")
        # Deliberately not open_page: the whole point is to look before the
        # data lands. The document being ready is the moment to look, and
        # the latency above is what keeps the answer from beating us there.
        self.wait_until(tools, "document.readyState === 'complete'",
                        note="the document to finish parsing")
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
            self.open_page(tools, self.base_url + page)
        self.assertEqual(tools.complaints, [], "the pages reported errors")

    def test_every_form_control_is_styled(self) -> None:
        """A control the stylesheet never reaches looks like 1995.

        The class-existence test next door cannot catch this. Every class on
        the name field of the Zigbee device page is defined -- the gap is
        that inputs are styled by descendant rules scoped to ancestors that
        field does not have, so nothing in the cascade reaches it. Asking
        "does this class have a rule" and asking "did any rule land on this
        element" are different questions, and only the browser can answer
        the second.

        The test is for the default look rather than a particular one: an
        unstyled control keeps the user agent's own border, which on this
        platform is a 2px inset in a colour no rule here ever sets.
        """
        tools = self.devtools()
        pages = ["/plants", f"/plants/{self.plant_id}/config",
                 f"/sensors/{self.sensor_id}/settings", "/settings",
                 "/settings/rooms", "/settings/zigbee",
                 # A device page, which exists whether or not that device
                 # does: its controls are drawn from the markup, and an
                 # unstyled one is unstyled either way.
                 "/zigbee/acebe6fffe0d7f18"]
        bare = []
        for page in pages:
            self.open_page(tools, self.base_url + page)
            found = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
                JSON.stringify([...document.querySelectorAll(
                    "main input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]), "
                    + "main select, main textarea")]
                  .filter(el => el.offsetParent !== null)
                  .filter(el => {
                    const style = getComputedStyle(el);
                    // A control nothing has styled keeps the user agent's
                    // inset border and no padding worth the name.
                    return style.borderStyle === "inset"
                        || (parseFloat(style.paddingLeft) < 4 && style.borderRadius === "0px");
                  })
                  .map(el => (el.id || el.name || el.tagName.toLowerCase())))""")["result"]["value"])
            bare.extend(f"{page}:{name}" for name in found)
        self.assertEqual(bare, [], f"form controls no rule reaches: {bare}")
        self.assertEqual(tools.complaints, [])

    def test_a_plant_page_draws_what_it_was_given(self) -> None:
        tools = self.devtools()
        self.open_page(tools, f"{self.base_url}/plants/{self.plant_id}")
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
        # Grams, the only unit the page has: 2100 g, not 2.10 kg.
        self.assertEqual(drawn["weight"], "2100")
        self.assertEqual(drawn["name"], "Spider")
        self.assertEqual(tools.complaints, [])

    def test_the_sensor_panel_knows_which_sensor_the_plant_has(self) -> None:
        """The page names the plant; the panel still has to find the sensor.

        Reading it from the address alone left the console switch with no
        sensor on it, so turning the console on called for nobody.
        """
        tools = self.devtools()
        # The sensor's settings are the sensor's page now, not its plant's.
        self.open_page(tools, f"{self.base_url}/sensors/{self.sensor_id}/settings")
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
        self.open_page(tools, f"{self.base_url}/sensors/{self.sensor_id}/settings")
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
        self.open_page(tools, f"{self.base_url}/plants/{self.plant_id}/config")
        # Changed the way a sheet changes it: a value and an input event,
        # with the field itself not focused.
        tools.call("Runtime.evaluate", expression="""
            const field = document.getElementById("plant-config-acquired");
            field.value = "2021-03-05";
            field.dispatchEvent(new Event("input", {bubbles: true}));
            document.body.focus();""")
        # Several polls, proven to have happened rather than waited out.
        self.after_a_poll(tools, rounds=3)
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
        self.open_page(tools, f"{self.base_url}/sensors/{scale_id}/settings")
        # Chosen the way a menu is chosen, then the pointer goes elsewhere.
        tools.call("Runtime.evaluate", expression="""
            const field = document.getElementById("scale-keep-every");
            field.value = "604800";
            field.dispatchEvent(new Event("change", {bubbles: true}));
            document.body.focus();""")
        # Several polls, proven to have happened rather than waited out.
        self.after_a_poll(tools, rounds=3)
        held = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({
              value: document.getElementById("scale-keep-every").value,
              focused: document.activeElement.id,
            })""")["result"]["value"])
        # Still the week that was picked, and not because the menu is held.
        self.assertEqual(held["value"], "604800")
        self.assertNotEqual(held["focused"], "scale-keep-every")
        self.assertEqual(tools.complaints, [])


    def zigbee_service(self, eui: str, interval: int):
        """A Zigbee service whose device keeps reporting the rate it has.

        Standing in for a scale that has been told to change and has not
        got round to it yet, which is the whole of the bug.
        """
        from open_plant_pulse_hub.application.zigbee import ZigbeeService
        from open_plant_pulse_hub.application.zigbee.network import Reading

        class Network:
            def devices(self): return []
            def notes(self, _eui): return {}
            def on_joined(self, handler): pass
            def on_delivery(self, handler): pass
            def on_reading(self, handler): self.handler = handler
            def send(self, *args, **kwargs): return 1

        network = Network()
        service = ZigbeeService(self.store, network)
        network.handler(Reading(eui=eui, kind="scale", values={
            "grams": 1500.0, "ok": True, "stable": True, "saturated": False,
            "wifi": True, "interval": interval, "batt": 3.9, "batt_pct": 70,
        }))
        return service

    def test_a_zigbee_rate_being_chosen_is_not_undone_by_the_device(self) -> None:
        """The same bug as above, on the page that had no such guard.

        A Zigbee device reports its rate with every reading, and the page
        wrote that back over the menu once a second. The command was on its
        way the whole time -- a sleepy device collects it on its own
        schedule -- so a setting that was working looked like one that
        refused to stick.
        """
        eui = "acebe6fffe0d7f18"
        self.store.seen_zigbee_device(eui, "scale")
        self.store.enroll_zigbee_device(eui, "Pot scale")
        # A device that is reporting, because the bug is the report being
        # written back over the menu. Without one there is nothing to
        # overwrite and the test passes whatever the page does.
        service = self.zigbee_service(eui, interval=60)
        server = create_server(self.store, "127.0.0.1", 0, zigbee=service)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                        daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2.0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server_address(server)

        tools = self.devtools()
        self.open_page(tools, f"http://{host}:{port}/zigbee/{eui}")
        tools.call("Runtime.evaluate", expression="""
            const field = document.getElementById("zigbee-scale-interval");
            field.value = "43200";
            field.dispatchEvent(new Event("change", {bubbles: true}));
            document.body.focus();""")
        self.after_a_poll(tools, rounds=3)
        held = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            JSON.stringify({
              value: document.getElementById("zigbee-scale-interval").value,
              focused: document.activeElement.id,
              says: document.getElementById("zigbee-scale-interval-state").textContent,
            })""")["result"]["value"])
        # Still twelve hours, and the page says it is on its way rather than
        # pretending the device already agreed.
        self.assertEqual(held["value"], "43200")
        self.assertNotEqual(held["focused"], "zigbee-scale-interval")
        self.assertIn("Sent", held["says"])
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
        self.open_page(tools, f"{self.base_url}/sensors/{scale_id}/settings")
        tools.call("Runtime.evaluate", expression="""
            window.__touched = [];
            document.getElementById("scale-keep-every").focus();
            new MutationObserver((muts) => {
              for (const m of muts) window.__touched.push(m.target.id || m.target.nodeName);
            }).observe(document.body,
                       {childList: true, subtree: true, attributes: true, characterData: true});
        """)
        # Several polls, including the once-a-second "last answered" counter.
        self.after_a_poll(tools, rounds=3)
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
        self.open_page(tools, f"{self.base_url}/sensors/{scale_id}/settings")
        tools.call("Runtime.evaluate", expression="""
            window.__flips = [];
            for (const section of document.querySelectorAll("[data-sensor-kind]")) {
              new MutationObserver(() => window.__flips.push(section.id))
                .observe(section, {attributes: true, attributeFilter: ["hidden"]});
            }""")
        # Several polls' worth: the page refreshes about once a second.
        self.after_a_poll(tools, rounds=3)
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

    def test_a_scale_page_draws_what_it_has_weighed(self) -> None:
        """Drift is read in the minutes between the weights that are kept.

        The chart is drawn from the device's own retained reports, not from
        the weights filed against a plant: those are one every few hours by
        design, and "has it settled since I moved it" lives in between.
        """
        eui = "acebe6fffe0d7f18"
        self.store.seen_zigbee_device(eui, "scale")
        now = datetime.now(timezone.utc)
        grams, drop = 15400.0, 60.0
        for i in range(40):
            at = (now - timedelta(minutes=5 * (40 - i))).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.store.record_zigbee_report(
                eui, {"grams": round(grams, 1), "stable": i % 7 == 0},
                kind="scale", lqi=10, rssi=-65, at=at)
            grams -= drop
            drop *= 0.88

        tools = self.devtools()
        tools.call("Page.navigate", url=f"{self.base_url}/zigbee/{eui}")
        time.sleep(4)
        drawn = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            (() => {
              const sec = document.getElementById("zigbee-weight-history");
              const svg = document.getElementById("zigbee-weight-chart");
              const batt = document.getElementById("zigbee-battery-history");
              return JSON.stringify({
                shown: !sec.hasAttribute("hidden") && !svg.hasAttribute("hidden"),
                settled: svg.querySelectorAll(".weight-point.settled").length,
                unsettled: svg.querySelectorAll(".weight-point.unsettled").length,
                summary: document.getElementById("zigbee-weight-summary").textContent,
                aboveBattery: !!(sec.compareDocumentPosition(batt) & 4),
              });
            })()""")["result"]["value"])

        self.assertTrue(drawn["shown"])
        self.assertTrue(drawn["aboveBattery"], "weight sits above the battery chart")
        # Unsettled readings are drawn, not dropped: a scale that never
        # settles is the thing somebody is looking for.
        self.assertEqual(drawn["settled"] + drawn["unsettled"], 40)
        self.assertGreater(drawn["unsettled"], 0)
        # Drift is quoted in grams whatever unit the weight is in; "0.02 kg
        # an hour" is no use to somebody watching a scale settle.
        self.assertIn("g an hour", drawn["summary"])
        self.assertIn("still settling", drawn["summary"])
        self.assertEqual(tools.complaints, [])

    PHONE_FIT = """
        (() => {
          const de = document.documentElement;
          const vw = de.clientWidth;
          // Something inside a scrolling box is allowed to reach past the
          // screen — that is what the box is for. Only unscrollable spill is
          // a page whose right-hand side the user cannot get to.
          const scrollable = (el) => {
            for (let p = el.parentElement; p; p = p.parentElement) {
              if (/auto|scroll/.test(getComputedStyle(p).overflowX)) return true;
            }
            return false;
          };
          const over = [...document.querySelectorAll("body *")]
            .filter((el) => el.getBoundingClientRect().width > 0)
            .filter((el) => el.getBoundingClientRect().right > vw + 1)
            .filter((el) => !scrollable(el));
          return JSON.stringify({
            viewport: vw,
            page: de.scrollWidth,
            // Only the outermost offender in each chain: its children are
            // carried along, not separately broken.
            spilled: over
              .filter((el) => !over.some((o) => o !== el && o.contains(el)))
              .map((el) => `${el.tagName.toLowerCase()}`
                + `${(el.className || "").toString().trim()
                     ? "." + el.className.toString().trim().split(/\\s+/)[0] : ""}`
                + `@${Math.round(el.getBoundingClientRect().right)}`),
          });
        })()"""

    def test_no_page_runs_off_the_side_of_a_phone(self) -> None:
        """Every page fits the screen it is read on.

        Each page is a grid item of <main>, and a grid item's default
        `min-width: auto` lets its min-content width beat the track: one row
        that will not wrap is enough to drag the entire page sideways. Seven
        settings tabs did it to 630px on a 390px screen, and the "Sort by" row
        did a smaller version of it to the household page. This sweeps the lot
        rather than checking the one page somebody happened to photograph.
        """
        tools = self.devtools()
        # 320 is the narrowest phone still in use, and it is where a fixed
        # floor shows up first; 390 is what most people are holding.
        for width in (320, 390):
            tools.call("Emulation.setDeviceMetricsOverride", width=width, height=844,
                       deviceScaleFactor=3, mobile=True)
            for page in ("/", "/plants", "/sensors", "/settings", "/settings/rooms",
                         "/settings/lights", "/settings/wifi", "/settings/time",
                         "/settings/firmware", "/settings/zigbee", "/onboarding"):
                with self.subTest(width=width, page=page):
                    self.open_page(tools, self.base_url + page)
                    fit = json.loads(tools.call(
                        "Runtime.evaluate", returnByValue=True,
                        expression=self.PHONE_FIT)["result"]["value"])
                    self.assertEqual(fit["viewport"], width)
                    self.assertEqual(
                        fit["page"], fit["viewport"],
                        f"{page} is {fit['page']}px wide on a {width}px screen;"
                        f" {fit['spilled']} spill over")
                    self.assertEqual(fit["spilled"], [], page)
        self.assertEqual(tools.complaints, [])

    def test_a_wide_table_scrolls_instead_of_widening_the_page(self) -> None:
        """Five columns do not fit a phone, and must not try to.

        The radio-neighbours table's min-content was 357px — a hard floor that
        held the whole settings page at 399px on every phone width, because a
        table will not shrink below its columns. The fix is a scrolling box
        around it, not narrower columns.
        """
        tools = self.devtools()
        tools.call("Emulation.setDeviceMetricsOverride", width=320, height=844,
                   deviceScaleFactor=3, mobile=True)
        self.open_page(tools, self.base_url + "/settings/zigbee")
        table = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            (() => {
              const de = document.documentElement;
              const table = document.getElementById("zigbee-neighbours");
              // The table is hidden until the gateway reports neighbours,
              // which is why a fixture with no radio data never showed this.
              table.removeAttribute("hidden");
              // A real neighbour row: a 16-hex-digit EUI in a monospace cell
              // is what makes the first column wide.
              table.tBodies[0].innerHTML =
                "<tr><td>acebe6fffe0d7f18</td><td>Router</td>"
                + "<td>0x0000</td><td>-65 dBm</td><td>132 of 255</td></tr>";
              const box = table.parentElement;
              const fits = () => ({
                page: de.scrollWidth,
                viewport: de.clientWidth,
                boxFits: box.getBoundingClientRect().width <= de.clientWidth + 1,
                scrolls: box.scrollWidth > Math.ceil(box.getBoundingClientRect().width),
              });
              const wrapping = fits();
              // Headers wrap, so the table usually squeezes in. Hex EUIs and
              // signal figures are what will not break: hold the cells on one
              // line and the table is genuinely wider than the phone.
              table.style.whiteSpace = "nowrap";
              const nowrap = fits();
              table.style.whiteSpace = "";
              return JSON.stringify({
                wrapping, nowrap,
                boxed: /auto|scroll/.test(getComputedStyle(box).overflowX),
                reachable: box.tabIndex >= 0,
              });
            })()""")["result"]["value"])

        self.assertTrue(table["boxed"], "the table sits in a box that can scroll")
        for state in ("wrapping", "nowrap"):
            with self.subTest(cells=state):
                seen = table[state]
                self.assertEqual(seen["page"], seen["viewport"],
                                 f"page is {seen['page']}px on a {seen['viewport']}px screen")
                self.assertTrue(seen["boxFits"])
        # Unbreakable cells are the case the box exists for.
        self.assertTrue(table["nowrap"]["scrolls"],
                        "columns that cannot wrap are scrollable to instead of overflowing")
        # Scrolling boxes are reached by keyboard too, as the raw report log
        # already is.
        self.assertTrue(table["reachable"], "the scrolling box takes focus")
        self.assertEqual(tools.complaints, [])

    def test_a_row_of_controls_wraps_rather_than_eating_the_margin(self) -> None:
        """Fitting on screen is not the same as being laid out.

        The household page's "Sort by … / Add a plant" row is one line that
        will not shrink. Without wrapping it stayed inside the viewport but
        overran its own section's padding, putting the button flush against
        the border — which reads as broken even though nothing is clipped.
        """
        tools = self.devtools()
        tools.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
                   deviceScaleFactor=3, mobile=True)
        self.open_page(tools, self.base_url + "/")
        row = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            (() => {
              const section = document.querySelector(".fleet-overview");
              const el = section.querySelector(".settings-sensor-actions");
              const cs = getComputedStyle(section);
              const box = section.getBoundingClientRect();
              // Where the section's content is allowed to reach.
              const edge = box.right - parseFloat(cs.paddingRight)
                                     - parseFloat(cs.borderRightWidth);
              const kids = [...el.children].map((k) => k.getBoundingClientRect());
              return JSON.stringify({
                edge: Math.round(edge),
                reach: Math.round(Math.max(...kids.map((k) => k.right))),
                lines: new Set(kids.map((k) => Math.round(k.top))).size,
              });
            })()""")["result"]["value"])

        self.assertGreaterEqual(
            row["edge"], row["reach"],
            f"controls reach {row['reach']}px, past the section's content edge"
            f" at {row['edge']}px")
        self.assertGreaterEqual(row["lines"], 2, "the row wrapped instead of overrunning")
        self.assertEqual(tools.complaints, [])

    def test_the_settings_tabs_stay_reachable_on_a_phone(self) -> None:
        """The tabs that do not fit are scrolled to, not clipped away."""
        tools = self.devtools()
        tools.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
                   deviceScaleFactor=3, mobile=True)
        self.open_page(tools, self.base_url + "/settings")
        bar = json.loads(tools.call("Runtime.evaluate", returnByValue=True, expression="""
            (() => {
              const de = document.documentElement;
              const bar = document.querySelector(".settings-tabs");
              const tabs = [...bar.querySelectorAll(".settings-tab")];
              const lines = (tab) => Math.round(
                tab.getBoundingClientRect().height /
                parseFloat(getComputedStyle(tab).lineHeight || "0"));
              return JSON.stringify({
                tabs: tabs.length,
                fits: bar.getBoundingClientRect().width <= de.clientWidth,
                scrolls: bar.scrollWidth > Math.ceil(bar.getBoundingClientRect().width),
                wrapped: tabs.filter((t) => lines(t) > 1).map((t) => t.textContent.trim()),
              });
            })()""")["result"]["value"])

        self.assertGreaterEqual(bar["tabs"], 7, "there are enough tabs to overflow")
        self.assertTrue(bar["fits"], "the bar itself is no wider than the screen")
        self.assertTrue(bar["scrolls"], "the tabs that do not fit are scrollable to")
        # "Wi-Fi" broke onto two lines rather than the bar admitting it was
        # out of room.
        self.assertEqual(bar["wrapped"], [])
        self.assertEqual(tools.complaints, [])

    def test_the_household_page_draws_a_tile_for_the_plant(self) -> None:
        tools = self.devtools()
        self.open_page(tools, self.base_url + "/plants")
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
