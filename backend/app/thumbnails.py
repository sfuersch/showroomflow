from __future__ import annotations

import io

from PIL import Image, ImageOps


THUMBNAIL_SIZE = (480, 360)
THUMBNAIL_QUALITY = 78


class ThumbnailError(RuntimeError):
    """An image could not be converted into a thumbnail."""


def create_thumbnail(image_bytes: bytes) -> bytes:
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS, reducing_gap=3.0)
    except (OSError, ValueError) as exc:
        raise ThumbnailError("Das Vorschaubild konnte nicht erzeugt werden") from exc
    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=THUMBNAIL_QUALITY,
        optimize=True,
        progressive=True,
    )
    return output.getvalue()


def thumbnail_key(source_object_key: str) -> str:
    stem, separator, _extension = source_object_key.rpartition(".")
    if not separator:
        stem = source_object_key
    return f"{stem}.thumbnail.jpg"
