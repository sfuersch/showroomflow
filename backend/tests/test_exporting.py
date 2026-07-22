import io
import zipfile

from PIL import Image
import pytest

from app.config import Settings
from app.exporting import (
    ExportItem,
    ExportValidationError,
    arrange_export_items,
    build_zip_bytes,
    validate_export_items,
)


def image_bytes(color: str, size: tuple[int, int] = (800, 600)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return output.getvalue()


class MemoryStorage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_object(self, *, object_key: str) -> bytes:
        return self.objects[object_key]


def test_zip_uses_continuous_vin_filenames_and_normalized_images() -> None:
    storage = MemoryStorage({"front": image_bytes("red"), "ad": image_bytes("blue")})
    items = [ExportItem(5, "Werbung", "ad"), ExportItem(1, "Front", "front")]

    archive = build_zip_bytes("VIN/123", items, storage, Settings())

    with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
        assert zip_file.namelist() == ["VIN_123_01.jpg", "VIN_123_02.jpg"]
        exported = Image.open(io.BytesIO(zip_file.read("VIN_123_01.jpg")))
        assert exported.size == (1920, 1440)


def test_duplicate_export_slot_is_rejected_with_both_names() -> None:
    with pytest.raises(ExportValidationError, match="Front und Werbung"):
        validate_export_items([ExportItem(5, "Front", "front"), ExportItem(5, "Werbung", "ad")])


def test_supplemental_image_keeps_position_and_photos_skip_it() -> None:
    arranged = arrange_export_items(
        [
            ExportItem(1, "Front", "front"),
            ExportItem(2, "Seite", "side"),
            ExportItem(3, "Heck", "rear"),
        ],
        [ExportItem(2, "Werbung", "ad")],
    )

    assert [(item.order, item.name) for item in arranged] == [
        (1, "Front"),
        (2, "Werbung"),
        (3, "Seite"),
        (4, "Heck"),
    ]


def test_missing_optional_photo_does_not_leave_a_numbering_gap() -> None:
    arranged = arrange_export_items(
        [ExportItem(1, "Front", "front"), ExportItem(3, "Heck", "rear")],
        [ExportItem(2, "Werbung", "ad")],
    )

    assert [(item.order, item.name) for item in arranged] == [
        (1, "Front"),
        (2, "Werbung"),
        (3, "Heck"),
    ]


def test_multiple_supplemental_images_keep_their_selected_positions() -> None:
    arranged = arrange_export_items(
        [
            ExportItem(1, "Front", "front"),
            ExportItem(2, "Seite", "side"),
            ExportItem(3, "Heck", "rear"),
        ],
        [
            ExportItem(2, "Angebot", "offer"),
            ExportItem(4, "Garantie", "warranty"),
        ],
    )

    assert [(item.order, item.name) for item in arranged] == [
        (1, "Front"),
        (2, "Angebot"),
        (3, "Seite"),
        (4, "Garantie"),
        (5, "Heck"),
    ]


def test_photos_skip_fixed_supplemental_positions_five_and_ten() -> None:
    arranged = arrange_export_items(
        [ExportItem(index, f"Foto {index}", f"photo-{index}") for index in range(1, 11)],
        [
            ExportItem(5, "Zusatzbild 5", "supplemental-5"),
            ExportItem(10, "Zusatzbild 10", "supplemental-10"),
        ],
    )

    assert [(item.order, item.name) for item in arranged] == [
        (1, "Foto 1"),
        (2, "Foto 2"),
        (3, "Foto 3"),
        (4, "Foto 4"),
        (5, "Zusatzbild 5"),
        (6, "Foto 5"),
        (7, "Foto 6"),
        (8, "Foto 7"),
        (9, "Foto 8"),
        (10, "Zusatzbild 10"),
        (11, "Foto 9"),
        (12, "Foto 10"),
    ]
