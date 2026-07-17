import io

from PIL import Image, ImageDraw

from app.processing import (
    CompositionOptions,
    apply_cutout_mask_to_original,
    compose_showroom,
)


def image_bytes(image: Image.Image, format_name: str) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


def test_showroom_composition_has_configured_output_size() -> None:
    background = Image.new("RGB", (800, 600), "#d7d7d7")
    vehicle = Image.new("RGBA", (500, 260), (0, 0, 0, 0))
    draw = ImageDraw.Draw(vehicle)
    draw.rounded_rectangle((20, 40, 480, 240), radius=45, fill=(25, 40, 70, 255))

    result = compose_showroom(
        image_bytes(background, "JPEG"),
        image_bytes(vehicle, "PNG"),
        CompositionOptions(width=1920, height=1440),
    )

    finished = Image.open(io.BytesIO(result))
    assert finished.format == "JPEG"
    assert finished.size == (1920, 1440)


def test_showroom_composition_rejects_empty_cutout() -> None:
    background = Image.new("RGB", (800, 600), "white")
    empty_vehicle = Image.new("RGBA", (500, 260), (0, 0, 0, 0))

    try:
        compose_showroom(
            image_bytes(background, "JPEG"),
            image_bytes(empty_vehicle, "PNG"),
            CompositionOptions(),
        )
    except RuntimeError as exc:
        assert "kein Fahrzeug" in str(exc)
    else:
        raise AssertionError("An empty cutout must not be accepted")


def test_preview_mask_preserves_original_resolution_and_pixels() -> None:
    original = Image.new("RGB", (1200, 800), (18, 42, 91))
    preview = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    ImageDraw.Draw(preview).rectangle((50, 40, 250, 180), fill=(200, 200, 200, 255))

    result = apply_cutout_mask_to_original(
        image_bytes(original, "JPEG"),
        image_bytes(preview, "PNG"),
    )

    restored = Image.open(io.BytesIO(result)).convert("RGBA")
    assert restored.size == original.size
    center_color = restored.getpixel((600, 400))[:3]
    assert all(abs(actual - expected) <= 1 for actual, expected in zip(center_color, (18, 42, 91)))
    assert restored.getpixel((0, 0))[3] == 0
    assert restored.getpixel((600, 400))[3] == 255
