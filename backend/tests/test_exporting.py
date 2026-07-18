import io
import zipfile

from PIL import Image
import pytest

from app.config import Settings
from app.exporting import (
    ExportItem,
    ExportValidationError,
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


def test_zip_uses_export_slots_for_vin_filenames_and_normalized_images() -> None:
    storage = MemoryStorage({"front": image_bytes("red"), "ad": image_bytes("blue")})
    items = [ExportItem(5, "Werbung", "ad"), ExportItem(1, "Front", "front")]

    archive = build_zip_bytes("VIN/123", items, storage, Settings())

    with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
        assert zip_file.namelist() == ["VIN_123_01.jpg", "VIN_123_05.jpg"]
        exported = Image.open(io.BytesIO(zip_file.read("VIN_123_01.jpg")))
        assert exported.size == (1920, 1440)


def test_duplicate_export_slot_is_rejected_with_both_names() -> None:
    with pytest.raises(ExportValidationError, match="Front und Werbung"):
        validate_export_items([ExportItem(5, "Front", "front"), ExportItem(5, "Werbung", "ad")])
