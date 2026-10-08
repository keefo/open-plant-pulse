"""Photographs of plants: the files on disk and the rows that describe them.

A plant's history is mostly numbers, and numbers do not show a new leaf or a
scorched one. The pictures are kept the way the firmware images are: the file
is named by the digest of its contents, so the same photograph uploaded twice
is stored once, and nothing can ask for a path that was not put there.

Only formats a browser will display are accepted, and the type is read from
the first bytes of the file rather than taken from whoever uploaded it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# A photograph the browser downsized before sending is a few hundred kilobytes;
# the ceiling is generous enough for one sent straight off a camera.
MAX_PHOTO_BYTES = 12 * 1024 * 1024

SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def sniff_image_type(data: bytes) -> str:
    """What this file actually is, read from its first bytes.

    A declared content type is whatever the sender felt like writing. The
    magic numbers are the file.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # Worth naming, because a photograph taken on an iPhone is one of these
    # and most browsers will not show it.
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1"):
        raise ValueError(
            "HEIC photographs cannot be shown in most browsers; export as JPEG first"
        )
    raise ValueError("that file is not a JPEG, PNG, WebP or GIF")


class PhotoLibrary:
    """Plant photographs on disk, with the store holding what they are of."""

    def __init__(self, store: Any, directory: Path | str) -> None:
        self._store = store
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def add(self, plant_id: int, data: bytes, caption: Optional[str] = None) -> Dict[str, Any]:
        if not data:
            raise ValueError("that upload was empty")
        if len(data) > MAX_PHOTO_BYTES:
            raise ValueError("that photograph is larger than 12 MB")
        content_type = sniff_image_type(data)
        digest = hashlib.sha256(data).hexdigest()
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self.path(digest, content_type)
        if not path.exists():
            # Written beside its destination and moved into place, so an
            # interrupted upload cannot leave a half file under a name the hub
            # would then serve.
            partial = path.with_suffix(path.suffix + ".partial")
            partial.write_bytes(data)
            partial.replace(path)
        return self._store.add_plant_photo(
            plant_id,
            digest,
            content_type,
            len(data),
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            caption,
        )

    def photos(self, plant_id: int) -> List[Dict[str, Any]]:
        return self._store.plant_photos(plant_id)

    def read(self, photo_id: int) -> Optional[Tuple[bytes, str]]:
        """The bytes to serve and what they are, or nothing."""
        photo = self._store.plant_photo(photo_id)
        if photo is None:
            return None
        path = self.path(photo["digest"], photo["content_type"])
        if not path.exists():
            return None
        return path.read_bytes(), photo["content_type"]

    def delete(self, photo_id: int) -> bool:
        """Forget a photograph, and remove the file once nothing wants it.

        Two plants may be shown the same photograph -- a shelf of them, filed
        under each -- and that is one file on disk. It goes only when the last
        row referring to it has gone.
        """
        photo = self._store.plant_photo(photo_id)
        if photo is None:
            return False
        self._store.delete_plant_photo(photo_id)
        if not self._store.photo_digest_in_use(photo["digest"]):
            path = self.path(photo["digest"], photo["content_type"])
            if path.exists():
                path.unlink()
        return True

    def path(self, digest: str, content_type: str) -> Path:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("photo digest must be 64 hexadecimal characters")
        suffix = SUFFIXES.get(content_type)
        if suffix is None:
            raise ValueError(f"unknown photo type: {content_type}")
        return self._directory / f"{digest}{suffix}"
