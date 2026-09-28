"""Keeping firmware images, and commanding a sensor to install one.

These tests build images rather than using a real one, so they run anywhere and
say exactly which byte makes an image acceptable or not.
"""

import hashlib
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.application.firmware import (
    APP_DESCRIPTOR_MAGIC,
    APP_DESCRIPTOR_OFFSET,
    DATE_OFFSET,
    IDF_VERSION_OFFSET,
    PROJECT_OFFSET,
    TIME_OFFSET,
    VERSION_OFFSET,
    FirmwareLibrary,
    parse_firmware_image,
)
from open_plant_pulse_hub.firmware_server import create_firmware_server, firmware_server_address
from open_plant_pulse_hub.ingestion.device_configuration import (
    FIRMWARE_STATUS_MARKER,
    FIRMWARE_UPDATE_PAYLOAD_SIZE,
    DeviceConfiguration,
    FirmwareUpdateCommand,
    decode_firmware_update,
    encode_device_configuration,
    encode_firmware_update,
    split_firmware_status,
    split_station_status,
)
from open_plant_pulse_hub.web import create_server, server_address


def build_image(
    version: str = "0.12.0",
    project: str = "open_plant_pulse",
    chip_id: int = 5,
    size: int = 0x2000,
    magic: int = 0xE9,
    descriptor_magic: int = APP_DESCRIPTOR_MAGIC,
) -> bytes:
    """An ESP-IDF application image, as far as anything here looks at one."""
    image = bytearray(b"\0" * size)
    image[0] = magic
    image[1] = 6
    image[12:14] = chip_id.to_bytes(2, "little")
    image[APP_DESCRIPTOR_OFFSET : APP_DESCRIPTOR_OFFSET + 4] = descriptor_magic.to_bytes(
        4, "little"
    )
    image[VERSION_OFFSET : VERSION_OFFSET + len(version)] = version.encode()
    image[PROJECT_OFFSET : PROJECT_OFFSET + len(project)] = project.encode()
    image[TIME_OFFSET : TIME_OFFSET + 8] = b"22:35:46"
    image[DATE_OFFSET : DATE_OFFSET + 11] = b"Sep 24 2026"
    image[IDF_VERSION_OFFSET : IDF_VERSION_OFFSET + 6] = b"v5.5.5"
    # Padding with a version-derived byte keeps two different versions from
    # hashing alike, which is what the digest is relied on for everywhere else.
    filler = (version.encode() or b"\x5a")[-1]
    for index in range(IDF_VERSION_OFFSET + 32, size):
        image[index] = filler
    return bytes(image)


class ImageParsingTests(unittest.TestCase):
    def test_reads_what_the_image_says_about_itself(self) -> None:
        image = parse_firmware_image(build_image(version="1.2.3"))

        self.assertEqual(image.version, "1.2.3")
        self.assertEqual(image.project, "open_plant_pulse")
        self.assertEqual(image.idf_version, "v5.5.5")
        self.assertEqual(image.built_at, "Sep 24 2026 22:35:46")
        self.assertEqual(image.size_bytes, 0x2000)

    def test_refuses_anything_that_is_not_this_project_on_this_chip(self) -> None:
        for description, data in (
            ("not an image", build_image(magic=0x00)),
            ("another chip", build_image(chip_id=9)),
            ("no descriptor", build_image(descriptor_magic=0x12345678)),
            ("another project", build_image(project="someone_elses_thing")),
            ("no version", build_image(version="")),
            ("truncated", build_image()[:100]),
            ("larger than a slot", build_image(size=0x190000 + 1)),
        ):
            with self.subTest(description), self.assertRaises(ValueError):
                parse_firmware_image(data)

    def test_the_real_built_image_is_accepted_when_one_is_present(self) -> None:
        built = Path(__file__).parents[2] / "sensor" / "build" / "open_plant_pulse.bin"
        if not built.exists():
            self.skipTest("no firmware has been built in this checkout")
        image = parse_firmware_image(built.read_bytes())

        self.assertEqual(image.project, "open_plant_pulse")
        version = (Path(__file__).parents[2] / "sensor" / "version.txt").read_text().strip()
        self.assertEqual(image.version, version)


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        self.directory = tempfile.TemporaryDirectory()
        self.library = FirmwareLibrary(self.store, Path(self.directory.name) / "firmware")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_stores_one_copy_per_image_and_serves_it_back(self) -> None:
        data = build_image(version="0.12.0")
        stored = self.library.add(data)
        again = self.library.add(data)

        self.assertEqual(stored["digest"], hashlib.sha256(data).hexdigest())
        self.assertEqual(stored["uploaded_at"], again["uploaded_at"])
        self.assertEqual(len(self.library.images()), 1)
        self.assertEqual(self.library.read(stored["digest"]), data)

    def test_serves_nothing_for_a_file_that_no_longer_matches_its_name(self) -> None:
        stored = self.library.add(build_image())
        self.library.path(stored["digest"]).write_bytes(build_image(version="9.9.9"))

        # The one place the hub hands bytes to a device that will run them.
        self.assertIsNone(self.library.read(stored["digest"]))

    def test_deleting_an_image_takes_the_file_and_frees_the_sensors_waiting(self) -> None:
        sensor_id = "sensor-aabbccddeeff"
        self.store.record_beacon(
            sensor_id=sensor_id,
            received_at="2026-09-25T10:00:00Z",
            observed_identifier="test",
            source_adapter="test",
            rssi=-40,
            service_data=b"\x40",
            contract_version=3,
        )
        self.store.manage_sensor(sensor_id, "Fern", "Office", "monstera", 40, 1500, 1200)
        stored = self.library.add(build_image())
        self.store.request_firmware_update(sensor_id, stored["digest"])

        self.assertTrue(self.library.delete(stored["digest"]))

        self.assertFalse(self.library.path(stored["digest"]).exists())
        sensor = self.store.sensor(sensor_id)
        self.assertEqual(sensor["firmware_update_state"], "failed")
        self.assertIn("removed from the hub", sensor["firmware_update_error"])

    def test_a_digest_is_the_only_thing_that_names_a_file(self) -> None:
        for candidate in ("../../etc/passwd", "", "zz" * 32, "abc"):
            with self.subTest(candidate), self.assertRaises(ValueError):
                self.library.path(candidate)


class CommandTests(unittest.TestCase):
    def command(self, **overrides) -> FirmwareUpdateCommand:
        values = {
            "update_id": 7,
            "size_bytes": 1294784,
            "address": "192.168.0.44",
            "port": 8081,
            "digest": "ab" * 32,
        }
        values.update(overrides)
        return FirmwareUpdateCommand(**values)

    def test_round_trips_through_forty_seven_bytes(self) -> None:
        payload = encode_firmware_update(self.command())

        self.assertEqual(len(payload), FIRMWARE_UPDATE_PAYLOAD_SIZE)
        self.assertEqual(decode_firmware_update(payload), self.command())

    def test_is_distinguishable_from_every_other_payload_on_the_characteristic(self) -> None:
        payload = encode_firmware_update(self.command())
        configuration = encode_device_configuration(
            DeviceConfiguration(
                revision=3, reporting_interval_seconds=60, plant_name="Fern", room="Office"
            )
        )

        # One characteristic carries several payloads, told apart by their first
        # byte. A new one that collided would be applied as the wrong thing.
        self.assertEqual(payload[0], 7)
        self.assertNotEqual(payload[0], configuration[0])
        self.assertNotEqual(payload[0], 5)  # release
        self.assertNotIn(payload[0], (0x20, 0x21, 0x22))  # bulk drain

    def test_refuses_a_command_a_sensor_could_not_act_on(self) -> None:
        for description, overrides in (
            ("no update", {"update_id": 0}),
            ("no image", {"size_bytes": 0}),
            ("no port", {"port": 0}),
            ("not an address", {"address": "imacpro.local"}),
            ("short digest", {"digest": "ab" * 16}),
            ("not hexadecimal", {"digest": "zz" * 32}),
        ):
            with self.subTest(description), self.assertRaises(ValueError):
                encode_firmware_update(self.command(**overrides))

    def test_progress_is_read_in_front_of_the_station_status(self) -> None:
        configuration = encode_device_configuration(
            DeviceConfiguration(
                revision=3, reporting_interval_seconds=60, plant_name="Fern", room="Office"
            )
        )
        progress = bytes((FIRMWARE_STATUS_MARKER, 2, 41, 0))
        station = bytes((0xA1, 1, 192, 168, 0, 111, 0, 12, 0))
        report = configuration + progress + station

        remainder, station_status = split_station_status(report)
        self.assertEqual(station_status, (True, "192.168.0.111", "0.12.0"))

        remainder, firmware_status = split_firmware_status(remainder)
        self.assertEqual(firmware_status, ("downloading", 41, None))
        # Whatever a hub does with the suffixes, the configuration underneath
        # them is unchanged, which is what keeps the older hub working.
        self.assertEqual(remainder, configuration)

    def test_a_report_without_progress_is_left_alone(self) -> None:
        station = bytes((0xA1, 0, 0, 0, 0, 0, 0, 11, 2))
        report = b"\x06payload-ish" + station

        remainder, _ = split_station_status(report)
        remainder, firmware_status = split_firmware_status(remainder)

        self.assertIsNone(firmware_status)
        self.assertEqual(remainder, b"\x06payload-ish")

    def test_every_failure_the_sensor_can_report_has_words(self) -> None:
        for reason in range(1, 8):
            report = b"\x06configuration" + bytes((FIRMWARE_STATUS_MARKER, 5, 0, reason))
            _, status = split_firmware_status(report)
            self.assertIsNotNone(status)
            self.assertEqual(status[0], "failed")
            self.assertTrue(status[2], f"failure {reason} has no explanation")


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        self.directory = tempfile.TemporaryDirectory()
        self.library = FirmwareLibrary(self.store, Path(self.directory.name) / "firmware")
        self.server = create_server(self.store, "127.0.0.1", 0, None, self.library)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"
        self.files = create_firmware_server(self.library, "127.0.0.1", 0)
        self.files_thread = Thread(target=self.files.serve_forever, daemon=True)
        self.files_thread.start()
        file_host, file_port = firmware_server_address(self.files)
        self.files_url = f"http://{file_host}:{file_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)
        self.files.shutdown()
        self.files.server_close()
        self.files_thread.join(timeout=1.0)
        self.store.close()
        self.directory.cleanup()

    def upload(self, data: bytes):
        request = Request(self.base_url + "/api/firmware", data=data, method="POST")
        with urlopen(request, timeout=5.0) as response:
            return json.load(response)

    def enrolled_sensor(self) -> str:
        sensor_id = "sensor-aabbccddeeff"
        self.store.record_beacon(
            sensor_id=sensor_id,
            received_at="2026-09-25T10:00:00Z",
            observed_identifier="test",
            source_adapter="test",
            rssi=-40,
            service_data=b"\x40",
            contract_version=3,
        )
        self.store.manage_sensor(sensor_id, "Fern", "Office", "monstera", 40, 1500, 1200)
        return sensor_id

    def test_an_image_is_uploaded_listed_and_served_to_a_sensor(self) -> None:
        data = build_image(version="0.12.0")
        stored = self.upload(data)

        self.assertEqual(stored["version"], "0.12.0")
        with urlopen(self.base_url + "/api/firmware", timeout=5.0) as response:
            listed = json.load(response)
        self.assertEqual([item["digest"] for item in listed["items"]], [stored["digest"]])
        self.assertTrue(listed["items"][0]["available"])

        with urlopen(f"{self.files_url}/firmware/{stored['digest']}.bin", timeout=5.0) as response:
            self.assertEqual(response.read(), data)

    def test_the_firmware_server_offers_nothing_else(self) -> None:
        stored = self.upload(build_image())
        for path in ("/", "/api/sensors", "/firmware/", "/firmware/../hub.sqlite3"):
            with self.subTest(path), self.assertRaises(HTTPError) as raised:
                urlopen(self.files_url + path, timeout=5.0)
            self.assertEqual(raised.exception.code, 404)
            raised.exception.close()
        # And a digest it does hold is still served, so the check above is not
        # passing because everything fails.
        with urlopen(f"{self.files_url}/firmware/{stored['digest']}.bin", timeout=5.0) as response:
            self.assertEqual(response.status, 200)

    def test_an_upload_that_is_not_firmware_is_refused_with_a_reason(self) -> None:
        with self.assertRaises(HTTPError) as raised:
            self.upload(b"this is a photograph" * 500)
        self.assertEqual(raised.exception.code, 400)
        self.assertIn("firmware", json.load(raised.exception)["error"])
        raised.exception.close()

    def test_a_sensor_is_asked_for_an_image_and_can_be_let_off_again(self) -> None:
        sensor_id = self.enrolled_sensor()
        stored = self.upload(build_image(version="0.12.0"))
        path = f"/api/sensors/{quote(sensor_id, safe='')}/firmware"

        request = Request(
            self.base_url + path,
            data=json.dumps({"digest": stored["digest"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5.0) as response:
            sensor = json.load(response)
        self.assertEqual(sensor["firmware_update_state"], "pending")
        self.assertEqual(sensor["firmware_update_version"], "0.12.0")
        self.assertEqual(
            self.store.pending_firmware_update(sensor_id)["digest"], stored["digest"]
        )

        request = Request(
            self.base_url + path,
            data=json.dumps({"digest": ""}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5.0) as response:
            sensor = json.load(response)
        self.assertEqual(sensor["firmware_update_state"], "idle")
        self.assertIsNone(self.store.pending_firmware_update(sensor_id))

    def test_deleting_an_image_removes_it_from_the_list(self) -> None:
        stored = self.upload(build_image())
        request = Request(
            self.base_url + "/api/firmware/" + stored["digest"], method="DELETE"
        )
        with urlopen(request, timeout=5.0) as response:
            self.assertEqual(response.status, 204)

        with urlopen(self.base_url + "/api/firmware", timeout=5.0) as response:
            self.assertEqual(json.load(response)["items"], [])


class InterfaceTests(unittest.TestCase):
    """The served page and script carry the pieces an update needs."""

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.server = create_server(self.store, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)
        self.store.close()

    def get(self, path: str) -> bytes:
        with urlopen(self.base_url + path, timeout=1.0) as response:
            return response.read()

    def test_images_are_managed_in_settings_and_installed_from_a_sensor(self) -> None:
        page = self.get("/")
        script = self.get("/app.js")

        self.assertIn(b'href="/settings/firmware"', page)
        self.assertIn(b'id="settings-firmware-panel"', page)
        self.assertIn(b'<section class="sensor-firmware" data-page="config"', page)
        self.assertIn(b'if (path === "/settings/firmware") return "firmware";', script)
        self.assertIn(b'fetch("/api/firmware", { method: "POST", body: file })', script)
        self.assertIn(b'/firmware", {\n    method: "POST"', script)

    def test_the_interface_refuses_to_start_what_the_sensor_could_not_finish(self) -> None:
        script = self.get("/app.js")

        # Downloading needs the household network, which is a precondition, not
        # something to discover halfway through.
        self.assertIn(b'selectedSensor.wifi_enabled && selectedSensor.wifi_state === "joined"', script)
        self.assertIn(b"setDisabled(install, running", script)
        self.assertIn(b"downloads firmware over the household network", script)

    def test_progress_is_what_the_sensor_said_and_names_every_state(self) -> None:
        script = self.get("/app.js")

        for state in ("pending", "commanded", "downloading", "installing", "rebooting",
                      "succeeded", "failed"):
            self.assertIn(f'case "{state}":'.encode(), script)
        self.assertIn(b"firmware_update_percent", script)


class StoreStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        self.directory = tempfile.TemporaryDirectory()
        self.library = FirmwareLibrary(self.store, Path(self.directory.name) / "firmware")
        self.sensor_id = "sensor-aabbccddeeff"
        self.store.record_beacon(
            sensor_id=self.sensor_id,
            received_at="2026-09-25T10:00:00Z",
            observed_identifier="test",
            source_adapter="test",
            rssi=-40,
            service_data=b"\x40",
            contract_version=3,
        )
        self.store.manage_sensor(self.sensor_id, "Fern", "Office", "monstera", 40, 1500, 1200)
        self.image = self.library.add(build_image(version="0.12.0"))

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_an_update_is_pending_only_until_the_sensor_is_told(self) -> None:
        self.store.request_firmware_update(self.sensor_id, self.image["digest"])
        self.assertIsNotNone(self.store.pending_firmware_update(self.sensor_id))

        self.store.record_firmware_update_state(self.sensor_id, "commanded")

        self.assertIsNone(self.store.pending_firmware_update(self.sensor_id))

    def test_asking_twice_asks_about_a_different_update(self) -> None:
        first = self.store.request_firmware_update(self.sensor_id, self.image["digest"])
        self.store.record_firmware_update_state(self.sensor_id, "failed", 0, "gave up")
        second = self.store.request_firmware_update(self.sensor_id, self.image["digest"])

        # The identifier is what lets a sensor ignore a repeat of the command it
        # is already running, and tell it apart from being asked again.
        self.assertGreater(second["firmware_update_id"], first["firmware_update_id"])
        self.assertIsNone(second["firmware_update_error"])

    def test_success_is_the_version_coming_back_not_the_sensor_saying_so(self) -> None:
        self.store.request_firmware_update(self.sensor_id, self.image["digest"])
        self.store.record_firmware_update_state(self.sensor_id, "rebooting", 100)

        self.store.record_station_report(self.sensor_id, "0.11.2")
        self.assertEqual(
            self.store.sensor(self.sensor_id)["firmware_update_state"], "rebooting"
        )

        self.store.record_station_report(self.sensor_id, "0.12.0")

        sensor = self.store.sensor(self.sensor_id)
        self.assertEqual(sensor["firmware_update_state"], "succeeded")
        self.assertEqual(sensor["firmware_update_percent"], 100)

    def test_an_update_the_sensor_never_returned_from_is_given_up_on(self) -> None:
        self.store.request_firmware_update(self.sensor_id, self.image["digest"])
        self.store.record_firmware_update_state(self.sensor_id, "rebooting", 100)

        # While it could still be on its way, nothing is concluded.
        self.assertIsNone(self.store.expire_stalled_firmware_update(self.sensor_id))

        self.store._database.execute(
            "UPDATE sensors SET firmware_update_started_at = '2026-01-01T00:00:00Z'"
        )
        self.store.record_station_report(self.sensor_id, "0.11.2")
        sensor = self.store.expire_stalled_firmware_update(self.sensor_id)

        # The commonest cause is the one this exists for: the new image did not
        # start, the sensor rolled back, and it is running what it had.
        self.assertEqual(sensor["firmware_update_state"], "failed")
        self.assertIn("did not come back", sensor["firmware_update_error"])
        self.assertIn("0.11.2", sensor["firmware_update_error"])
        self.assertIsNone(self.store.expire_stalled_firmware_update(self.sensor_id))

    def test_only_an_enrolled_sensor_and_a_stored_image_can_be_asked_for(self) -> None:
        with self.assertRaises(ValueError):
            self.store.request_firmware_update(self.sensor_id, "ab" * 32)
        with self.assertRaises(ValueError):
            self.store.request_firmware_update("sensor-missing", self.image["digest"])

    def test_an_unknown_state_is_refused_rather_than_stored(self) -> None:
        with self.assertRaises(ValueError):
            self.store.record_firmware_update_state(self.sensor_id, "nearly-there")


if __name__ == "__main__":
    unittest.main()
