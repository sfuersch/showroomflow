import io

import httpx
import pytest
from PIL import Image, ImageDraw

from app.config import Settings
from app.processing import (
    CompositionOptions,
    OverlayLayer,
    VehicleContour,
    apply_cutout_mask_to_original,
    apply_image_overlays,
    calculate_contour_framing,
    compose_showroom,
    create_photoroom_showroom,
    measure_vehicle_contour,
)


def image_bytes(image: Image.Image, format_name: str) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


@pytest.mark.parametrize(
    ("contour", "expected_width", "expected_height"),
    [
        (VehicleContour(1000, 1000), 0.520, 0.693),
        (VehicleContour(1400, 1000), 0.615, 0.586),
        (VehicleContour(2000, 1000), 0.735, 0.490),
    ],
)
def test_contour_framing_normalizes_visible_vehicle_area(
    contour: VehicleContour,
    expected_width: float,
    expected_height: float,
) -> None:
    framing = calculate_contour_framing(
        contour,
        output_width=1920,
        output_height=1440,
        target_area_percent=36,
        max_width_percent=78,
        max_height_percent=72,
    )

    assert framing.width_fraction == pytest.approx(expected_width, abs=0.001)
    assert framing.height_fraction == pytest.approx(expected_height, abs=0.001)
    assert framing.width_fraction * framing.height_fraction == pytest.approx(0.36, abs=0.001)


def test_contour_framing_respects_maximum_dimensions() -> None:
    framing = calculate_contour_framing(
        VehicleContour(3000, 800),
        output_width=1920,
        output_height=1440,
        target_area_percent=36,
        max_width_percent=78,
        max_height_percent=72,
    )

    assert framing.width_fraction == pytest.approx(0.78)
    assert framing.height_fraction < 0.30


def test_vehicle_contour_ignores_faint_transparent_pixels() -> None:
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(cutout)
    draw.rectangle((0, 0, 799, 599), fill=(255, 255, 255, 40))
    draw.rectangle((180, 120, 619, 519), fill=(30, 30, 30, 255))

    contour = measure_vehicle_contour(image_bytes(cutout, "PNG"))

    assert contour == VehicleContour(width=440, height=400)


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


def test_overlay_is_scaled_and_placed_on_optimized_image() -> None:
    base = Image.new("RGB", (400, 300), "white")
    logo = Image.new("RGBA", (100, 50), (210, 20, 30, 255))

    result = apply_image_overlays(
        image_bytes(base, "JPEG"),
        [
            OverlayLayer(
                content=image_bytes(logo, "PNG"),
                position="bottom_right",
                width_percent=20,
                opacity_percent=100,
            )
        ],
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    assert finished.size == (400, 300)
    assert finished.getpixel((330, 255))[0] > 180
    assert finished.getpixel((20, 20))[0] > 240


def test_overlay_opacity_is_applied_without_changing_canvas_size() -> None:
    base = Image.new("RGB", (400, 300), "white")
    logo = Image.new("RGBA", (100, 100), (0, 0, 0, 255))

    result = apply_image_overlays(
        image_bytes(base, "JPEG"),
        [OverlayLayer(image_bytes(logo, "PNG"), "center", 25, 50)],
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    center = finished.getpixel((200, 150))[0]
    assert 110 <= center <= 145
    assert finished.size == (400, 300)


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
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((200, 100, 599, 499), fill=(20, 30, 40, 255))
    cutout_result = image_bytes(cutout, "PNG")
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url == "https://image-api.photoroom.com/v2/edit"
        assert request.headers["x-api-key"] == "sandbox_test-key"
        assert request.headers["pr-hd-background-removal"] == "auto"
        body = request.content
        assert b'name="imageFile"' in body
        if requests == 1:
            assert b'name="referenceBox"' in body
            assert b"originalImage" in body
            assert b'name="export.format"' in body
            assert b"png" in body
            assert b'name="background.imageFile"' not in body
            return httpx.Response(
                200,
                content=cutout_result,
                headers={"content-type": "image/png"},
            )
        assert b'name="background.imageFile"' in body
        assert b'name="background.color"' in body
        assert b"FFFFFF" in body
        assert b'name="shadow.mode"' in body
        assert b"ai.soft" in body
        assert b'name="outputSize"' in body
        assert b"1920x1440" in body
        assert b'name="paddingLeft"' in body
        assert b"0.240" in body
        assert b'name="paddingTop"' in body
        assert b"0.207" in body
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
            vehicle_bottom_percent=90,
            client=client,
        )

    finished = Image.open(io.BytesIO(result))
    assert finished.format == "JPEG"
    assert finished.size == (1920, 1440)
    assert requests == 2


def test_optimized_photoroom_request_preserves_color_and_consistent_positioning() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    api_result = image_bytes(Image.new("RGB", (1920, 1440), "gray"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((120, 150, 679, 449), fill=(20, 30, 40, 255))
    cutout_result = image_bytes(cutout, "PNG")
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        body = request.content
        if requests == 1:
            return httpx.Response(
                200,
                content=cutout_result,
                headers={"content-type": "image/png"},
            )
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
    assert requests == 2
