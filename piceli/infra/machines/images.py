"""Stage an :class:`~piceli.infra.machines.model.Image` for upload, checksum verified.

The rendered configuration names an uploaded image by its checksum only
(``images/<sha256>.<format>``, relative to the state directory), so it holds
no local path or URL. Right before ``tofu apply`` creates the image, Piceli
copies the file (or downloads the URL) into that place, hashing what it
writes, and refuses a different ``sha256`` (``infra-image-checksum``) before
anything is uploaded. The staged copies are removed when the apply ends,
whatever its outcome.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO

from piceli.infra.machines.model import Image, InfraError

__all__ = ["stage_image", "unstage_images"]

STAGE = "images"
_CHUNK = 1024 * 1024


def _copy(source: BinaryIO, target: Path) -> str:
    hasher = hashlib.sha256()
    with target.open("wb") as handle:
        while chunk := source.read(_CHUNK):
            hasher.update(chunk)
            handle.write(chunk)
    return hasher.hexdigest()


def stage_image(image: Image, directory: Path) -> Path:
    """Copy or download ``image`` under ``directory`` and verify its sha256."""
    target = directory / image.local_path
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_name(target.name + ".partial")
    try:
        try:
            if image.file is not None:
                with Path(image.file).open("rb") as source:
                    digest = _copy(source, partial)
            else:
                assert image.url is not None
                request = urllib.request.Request(image.url, method="GET")
                with urllib.request.urlopen(request, timeout=60) as response:
                    digest = _copy(response, partial)
        except (OSError, ValueError, urllib.error.URLError):
            raise InfraError(
                "infra-image-unavailable",
                f"image {image.name}: its file= or url= could not be read",
            ) from None
        if digest != image.sha256:
            raise InfraError(
                "infra-image-checksum",
                f"image {image.name}: the sha256 is not the declared one",
            )
        os.replace(partial, target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def unstage_images(directory: Path) -> None:
    """Remove every staged image under ``directory``."""
    shutil.rmtree(directory / STAGE, ignore_errors=True)
