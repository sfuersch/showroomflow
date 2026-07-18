import io

import pytest
from PIL import Image

from app.thumbnails import ThumbnailError, create_thumbnail, thumbnail_key


def _image_bytes(size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "navy").save(output, format="JPEG", quality=95)
    return output.getvalue()


@pytest.mark.parametrize(
    ("source_size", "expected_size"),
    [
        ((4032, 3024), (480, 360)),
        ((3024, 4032), (270, 360)),
        ((320, 240), (320, 240)),
    ],
)
def test_thumbnail_preserves_aspect_ratio_and_never_upscales(
    source_size: tuple[int, int],
    expected_size: tuple[int, int],
) -> None:
    result = create_thumbnail(_image_bytes(source_size))

    thumbnail = Image.open(io.BytesIO(result))
    assert thumbnail.format == "JPEG"
    assert thumbnail.size == expected_size
    assert len(result) < len(_image_bytes(source_size))


def test_thumbnail_rejects_invalid_image() -> None:
    with pytest.raises(ThumbnailError):
        create_thumbnail(b"not-an-image")


def test_thumbnail_key_is_stable() -> None:
    assert thumbnail_key("jobs/one/photo.jpg") == "jobs/one/photo.thumbnail.jpg"
