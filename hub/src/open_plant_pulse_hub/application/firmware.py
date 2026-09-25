"""Firmware images the hub keeps, and what it will accept as one.

An uploaded file is not firmware because somebody said so. Every ESP-IDF
application carries a descriptor naming the chip it was built for, the project it
belongs to and its version, so the hub reads those out of the file itself and
refuses anything else. The version a sensor is offered is then the version that
sensor will report after it boots, rather than whatever was typed into a form.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

# Layout of an ESP-IDF application image: a 24-byte image header, an 8-byte
# segment header, then esp_app_desc_t.
IMAGE_MAGIC = 0xE9
ESP32C3_CHIP_ID = 5
APP_DESCRIPTOR_OFFSET = 0x20
APP_DESCRIPTOR_MAGIC = 0xABCD5432
VERSION_OFFSET = APP_DESCRIPTOR_OFFSET + 0x10
PROJECT_OFFSET = APP_DESCRIPTOR_OFFSET + 0x30
TIME_OFFSET = APP_DESCRIPTOR_OFFSET + 0x50
DATE_OFFSET = APP_DESCRIPTOR_OFFSET + 0x60
IDF_VERSION_OFFSET = APP_DESCRIPTOR_OFFSET + 0x70
PROJECT_NAME = "open_plant_pulse"
# Two OTA slots of 1600 KiB each. An image larger than a slot cannot be
# installed, and finding that out on the sensor would waste the whole download.
MAX_IMAGE_BYTES = 0x190000
MIN_IMAGE_BYTES = 0x1000


@dataclass(frozen=True)
class FirmwareImage:
    digest: str
    version: str
    project: str
    idf_version: str
    size_bytes: int
    built_at: str


def parse_firmware_image(data: bytes) -> FirmwareImage:
    """Read an ESP-IDF application image, or say why it is not one."""
    if len(data) < IDF_VERSION_OFFSET + 32:
        raise ValueError("file is too small to be a firmware image")
    if not MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError(
            f"firmware image must be between {MIN_IMAGE_BYTES} and {MAX_IMAGE_BYTES} bytes"
        )
    if data[0] != IMAGE_MAGIC:
        raise ValueError("file is not an ESP-IDF firmware image")
    chip_id = int.from_bytes(data[12:14], "little")
    if chip_id != ESP32C3_CHIP_ID:
        raise ValueError(f"firmware image was built for chip {chip_id}, not the ESP32-C3")
    if int.from_bytes(data[APP_DESCRIPTOR_OFFSET : APP_DESCRIPTOR_OFFSET + 4], "little") != (
        APP_DESCRIPTOR_MAGIC
    ):
        raise ValueError("firmware image carries no application descriptor")
    project = _descriptor_text(data, PROJECT_OFFSET, 32)
    if project != PROJECT_NAME:
        raise ValueError(f"firmware image belongs to project '{project}', not {PROJECT_NAME}")
    version = _descriptor_text(data, VERSION_OFFSET, 32)
    if not version:
        raise ValueError("firmware image carries no version")
    built_at = " ".join(
        part
        for part in (
            _descriptor_text(data, DATE_OFFSET, 16),
            _descriptor_text(data, TIME_OFFSET, 16),
        )
        if part
    )
    return FirmwareImage(
        digest=hashlib.sha256(data).hexdigest(),
        version=version,
        project=project,
        idf_version=_descriptor_text(data, IDF_VERSION_OFFSET, 32),
        size_bytes=len(data),
        built_at=built_at,
    )


def _descriptor_text(data: bytes, offset: int, size: int) -> str:
    field = data[offset : offset + size].split(b"\0", 1)[0]
    try:
        return field.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("firmware descriptor is not valid UTF-8") from error


class FirmwareLibrary:
    """The images on disk and the rows that describe them, kept in step.

    The digest is the identity. Uploading the same image twice stores one copy,
    and a sensor is commanded to fetch a digest rather than a file name, so
    nothing here can be persuaded to serve something else.
    """

    def __init__(self, store: Any, directory: Path | str) -> None:
        self._store = store
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def add(self, data: bytes) -> Dict[str, Any]:
        image = parse_firmware_image(data)
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self.path(image.digest)
        if not path.exists():
            # Written beside its destination and moved into place, so a failed or
            # interrupted upload cannot leave a half file that hashes to a name
            # the hub would then serve.
            partial = path.with_suffix(".partial")
            partial.write_bytes(data)
            partial.replace(path)
        record = dict(asdict(image))
        record["uploaded_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return self._store.add_firmware_image(record)

    def images(self) -> List[Dict[str, Any]]:
        return [self._with_availability(image) for image in self._store.firmware_images()]

    def image(self, digest: str) -> Optional[Dict[str, Any]]:
        image = self._store.firmware_image(digest)
        return None if image is None else self._with_availability(image)

    def read(self, digest: str) -> Optional[bytes]:
        """The bytes to serve, or nothing if the digest names no stored image."""
        image = self._store.firmware_image(digest)
        if image is None:
            return None
        path = self.path(digest)
        if not path.exists():
            return None
        data = path.read_bytes()
        # The file on disk is checked against the name it is stored under rather
        # than trusted, because this is the one place the hub hands bytes to a
        # device that will run them.
        if hashlib.sha256(data).hexdigest() != digest:
            return None
        return data

    def delete(self, digest: str) -> bool:
        removed = self._store.delete_firmware_image(digest)
        path = self.path(digest)
        if path.exists():
            path.unlink()
        return removed

    def path(self, digest: str) -> Path:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("firmware digest must be 64 hexadecimal characters")
        return self._directory / f"{digest}.bin"

    def _with_availability(self, image: Dict[str, Any]) -> Dict[str, Any]:
        image = dict(image)
        image["available"] = self.path(image["digest"]).exists()
        return image
