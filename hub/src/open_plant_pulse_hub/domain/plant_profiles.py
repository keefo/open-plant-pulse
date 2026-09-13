import json
from pathlib import Path
import re
from typing import Any, Dict, Optional, Sequence


PROFILE_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "plant_profiles"
PROFILE_SCHEMA_VERSION = 1


def _read_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read plant profile data {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Plant profile data {path.name} must be a JSON object")
    if value.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"Plant profile data {path.name} requires schema_version {PROFILE_SCHEMA_VERSION}")
    return value


def _number_pair(value: Any, field: str, path: Path) -> Sequence[float]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(item, (int, float)) for item in value)
        or value[0] >= value[1]
    ):
        raise ValueError(f"Plant profile {path.name} field {field} must be an increasing number pair")
    return value


def _validate_profile(profile: Dict[str, Any], path: Path) -> None:
    profile_id = profile.get("id")
    if not isinstance(profile_id, str) or re.fullmatch(r"[a-z0-9_]+", profile_id) is None:
        raise ValueError(f"Plant profile {path.name} id must use lowercase letters, numbers, and underscores")
    if profile_id != path.stem:
        raise ValueError(f"Plant profile {path.name} id must match its filename")
    for field in ("name", "scientific_name", "root_zone", "watering", "drainage", "climate", "chemistry"):
        if field not in profile:
            raise ValueError(f"Plant profile {path.name} is missing {field}")

    for section_name in ("root_zone", "climate", "chemistry"):
        section = profile[section_name]
        if not isinstance(section, dict) or not section:
            raise ValueError(f"Plant profile {path.name} section {section_name} must not be empty")
        for metric_name, metric in section.items():
            if not isinstance(metric, dict) or "label" not in metric or "unit" not in metric:
                raise ValueError(f"Plant profile {path.name} metric {metric_name} requires label and unit")
            scale = _number_pair(metric.get("scale"), f"{section_name}.{metric_name}.scale", path)
            if metric_name != "moisture_percent":
                ideal = _number_pair(metric.get("ideal"), f"{section_name}.{metric_name}.ideal", path)
                if ideal[0] < scale[0] or ideal[1] > scale[1]:
                    raise ValueError(f"Plant profile {path.name} metric {metric_name} ideal must be inside scale")

    watering = profile["watering"]
    for field in ("strategy", "label", "refill_below", "concern_below", "concern_above"):
        if field not in watering:
            raise ValueError(f"Plant profile {path.name} watering requires {field}")
    target = _number_pair(watering.get("post_water_target"), "watering.post_water_target", path)
    cycle = _number_pair(watering.get("comfortable_cycle"), "watering.comfortable_cycle", path)
    thresholds = [
        watering["concern_below"],
        watering["refill_below"],
        cycle[1],
        watering["concern_above"],
    ]
    if any(not isinstance(item, (int, float)) for item in thresholds) or thresholds != sorted(thresholds):
        raise ValueError(f"Plant profile {path.name} watering thresholds must run from dry to wet")
    if target[0] < cycle[0] or target[1] > cycle[1]:
        raise ValueError(f"Plant profile {path.name} post-water target must be inside its comfortable cycle")

    drainage = profile["drainage"]
    if not isinstance(drainage, dict):
        raise ValueError(f"Plant profile {path.name} drainage must be an object")
    response_classes = {"fast", "balanced", "retaining"}
    preferred = drainage.get("preferred_response")
    acceptable = drainage.get("acceptable_responses")
    maximum_settle = drainage.get("maximum_settle_minutes")
    if preferred not in response_classes:
        raise ValueError(f"Plant profile {path.name} drainage preferred_response is invalid")
    if (
        not isinstance(acceptable, list)
        or not acceptable
        or any(item not in response_classes for item in acceptable)
        or preferred not in acceptable
    ):
        raise ValueError(f"Plant profile {path.name} drainage acceptable_responses must include the preferred response")
    if not isinstance(maximum_settle, (int, float)) or maximum_settle <= 0:
        raise ValueError(f"Plant profile {path.name} drainage maximum_settle_minutes must be positive")
    if not isinstance(drainage.get("label"), str) or not drainage["label"]:
        raise ValueError(f"Plant profile {path.name} drainage requires a label")


def load_plant_profiles(data_dir: Optional[Path] = None) -> Dict[str, Any]:
    directory = data_dir or PROFILE_DATA_DIR
    catalog = _read_object(directory / "_catalog.json")
    profiles: Dict[str, Dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        profile = _read_object(path)
        _validate_profile(profile, path)
        profile_id = profile.pop("id")
        profile.pop("$schema", None)
        profile.pop("schema_version", None)
        if profile_id in profiles:
            raise ValueError(f"Duplicate plant profile id {profile_id}")
        profiles[profile_id] = profile

    default_profile = catalog.get("default_profile")
    if default_profile not in profiles:
        raise ValueError(f"Catalog default_profile {default_profile!r} does not exist")
    levels = catalog.get("assessment_policy", {}).get("levels")
    if not isinstance(levels, list) or not levels:
        raise ValueError("Catalog assessment_policy requires at least one level")

    catalog.pop("schema_version", None)
    catalog["profiles"] = profiles
    return catalog


def public_plant_profiles() -> Dict[str, Any]:
    return load_plant_profiles()