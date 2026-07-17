import io

import httpx
from PIL import Image, ImageDraw

from app.config import Settings
from app.processing import (
    CompositionOptions,
    apply_cutout_mask_to_original,
    compose_showroom,
    create_photoroom_showroom,
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


def test_photoroom_sandbox_request_keeps_comparison_separate() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    api_result = image_bytes(Image.new("RGB", (1920, 1440), "gray"), "JPEG")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://image-api.photoroom.com/v2/edit"
        assert request.headers["x-api-key"] == "sandbox_test-key"
        assert request.headers["pr-hd-background-removal"] == "auto"
        body = request.content
        assert b'name="imageFile"' in body
        assert b'name="background.imageFile"' in body
        assert b'name="background.color"' in body
        assert b"FFFFFF" in body
        assert b'name="shadow.mode"' in body
        assert b"ai.soft" in body
        assert b'name="outputSize"' in body
        assert b"1920x1440" in body
        assert b'name="paddingLeft"' in body
        assert b"0.200" in body
        assert b'name="verticalAlignment"' in body
        assert b"bottom" in body
        assert b"lighting.mode" not in body
        return httpx.Response(200, content=api_result, headers={"content-type": "image/jpeg"})

    settings = Settings(photoroom_api_key="test-key", photoroom_sandbox=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_photoroom_showroom(
            original,
            background,
            "image/jpeg",
            settings,
            vehicle_scale_percent=60,
            vehicle_bottom_percent=90,
            client=client,
        )

    finished = Image.open(io.BytesIO(result))
    assert finished.format == "JPEG"
    assert finished.size == (1920, 1440)


def test_optimized_photoroom_request_preserves_color_and_consistent_positioning() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    api_result = image_bytes(Image.new("RGB", (1920, 1440), "gray"), "JPEG")

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        assert b'name="lighting.mode"' in body
        assert b"ai.preserve-hue-and-saturation" in body
        assert b'name="ignorePaddingAndSnapOnCroppedSides"' in body
        assert b"false" in body
        assert b"ai.auto" not in body
        return httpx.Response(200, content=api_result, headers={"content-type": "image/jpeg"})

    settings = Settings(photoroom_api_key="test-key", photoroom_sandbox=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_photoroom_showroom(
            original,
            background,
            "image/jpeg",
            settings,
            optimized=True,
            client=client,
        )

    assert Image.open(io.BytesIO(result)).size == (1920, 1440)
