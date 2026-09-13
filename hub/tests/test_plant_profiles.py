import json
from pathlib import Path
import tempfile
import unittest

from open_plant_pulse_hub.domain.plant_profiles import PROFILE_DATA_DIR, load_plant_profiles


class PlantProfileLoaderTests(unittest.TestCase):
    def test_discovers_every_profile_file(self) -> None:
        catalog = load_plant_profiles()

        self.assertEqual(set(catalog["profiles"]), {"generic_foliage", "monstera", "strelitzia"})
        self.assertNotIn("schema_version", catalog["profiles"]["strelitzia"])
        self.assertNotIn("$schema", catalog["profiles"]["strelitzia"])
        self.assertEqual(catalog["profiles"]["monstera"]["drainage"]["preferred_response"], "balanced")

    def test_discovers_new_file_without_python_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            catalog = json.loads((PROFILE_DATA_DIR / "_catalog.json").read_text(encoding="utf-8"))
            source = json.loads((PROFILE_DATA_DIR / "strelitzia.json").read_text(encoding="utf-8"))
            (directory / "_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            (directory / "strelitzia.json").write_text(json.dumps(source), encoding="utf-8")
            source["id"] = "community_plant"
            source["name"] = "Community plant"
            (directory / "community_plant.json").write_text(json.dumps(source), encoding="utf-8")

            loaded = load_plant_profiles(directory)

            self.assertEqual(set(loaded["profiles"]), {"community_plant", "strelitzia"})

    def test_rejects_filename_id_mismatch_with_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            catalog = json.loads((PROFILE_DATA_DIR / "_catalog.json").read_text(encoding="utf-8"))
            profile = json.loads((PROFILE_DATA_DIR / "strelitzia.json").read_text(encoding="utf-8"))
            (directory / "_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            (directory / "wrong_name.json").write_text(json.dumps(profile), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "wrong_name.json id must match"):
                load_plant_profiles(directory)

    def test_rejects_incoherent_watering_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            catalog = json.loads((PROFILE_DATA_DIR / "_catalog.json").read_text(encoding="utf-8"))
            profile = json.loads((PROFILE_DATA_DIR / "strelitzia.json").read_text(encoding="utf-8"))
            profile["watering"]["refill_below"] = 90
            (directory / "_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            (directory / "strelitzia.json").write_text(json.dumps(profile), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "thresholds must run from dry to wet"):
                load_plant_profiles(directory)

    def test_rejects_drainage_preferences_outside_acceptable_responses(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            catalog = json.loads((PROFILE_DATA_DIR / "_catalog.json").read_text(encoding="utf-8"))
            profile = json.loads((PROFILE_DATA_DIR / "strelitzia.json").read_text(encoding="utf-8"))
            profile["drainage"]["acceptable_responses"] = ["fast"]
            (directory / "_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            (directory / "strelitzia.json").write_text(json.dumps(profile), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must include the preferred response"):
                load_plant_profiles(directory)


if __name__ == "__main__":
    unittest.main()