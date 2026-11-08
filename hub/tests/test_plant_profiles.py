import json
from pathlib import Path
import tempfile
import unittest

from open_plant_pulse_hub.domain.plant_profiles import PROFILE_DATA_DIR, load_plant_profiles


class PlantProfileLoaderTests(unittest.TestCase):
    def test_discovers_every_profile_file(self) -> None:
        catalog = load_plant_profiles()

        self.assertEqual(
            set(catalog["profiles"]),
            {
                "alocasia",
                "begonia",
                "dracaena",
                "generic_foliage",
                "jasmine",
                "monstera",
                "pothos",
                "spider_plant",
                "strelitzia",
                "tillandsia",
            },
        )
        self.assertNotIn("schema_version", catalog["profiles"]["strelitzia"])
        self.assertNotIn("$schema", catalog["profiles"]["strelitzia"])
        self.assertEqual(catalog["profiles"]["monstera"]["drainage"]["preferred_response"], "balanced")

    def test_every_profile_matches_the_schema_it_names(self) -> None:
        """The schema sits beside them and nothing was holding them to it.

        Each profile points at _profile.schema.json for an editor to
        validate against, and two of them did not match: a light
        band's effects are the bottom, middle and top third of that
        band, three sentences, and alocasia and begonia had two and
        one. An editor would have said so; no check ever did, so the
        file said one thing and the data another for as long as it
        took somebody to add the eleventh profile.
        """
        try:
            import jsonschema
        except ImportError:  # pragma: no cover - only on a bare checkout
            self.skipTest("jsonschema is not installed")

        schema = json.loads(
            (PROFILE_DATA_DIR / "_profile.schema.json").read_text(encoding="utf-8"))
        files = sorted(f for f in PROFILE_DATA_DIR.glob("*.json")
                       if not f.name.startswith("_"))
        self.assertGreater(len(files), 5, "hardly any profiles were found to check")
        wrong = []
        for path in files:
            try:
                jsonschema.validate(json.loads(path.read_text(encoding="utf-8")), schema)
            except jsonschema.ValidationError as error:
                where = "/".join(str(part) for part in error.absolute_path)
                wrong.append(f"{path.name} at {where}: {error.message}")
        self.assertEqual(wrong, [], "these profiles do not match the schema they name")

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