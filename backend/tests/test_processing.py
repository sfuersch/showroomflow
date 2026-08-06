import base64
import io
import uuid
from dataclasses import replace

import cv2
import httpx
import numpy as np
import pytest
from PIL import Image, ImageChops, ImageDraw

from app.config import Settings
from app.api_usage import ExternalApiUsageContext
import app.processing as processing_module
from app.models import Background, BackgroundOrientationComposition
from app.processing import (
    BackgroundComposition,
    CompositionOptions,
    ImageProcessingError,
    OverlayLayer,
    SceneAdjustment,
    VehicleContour,
    VehicleFrame,
    apply_cutout_mask_to_original,
    background_mask_from_cutout,
    apply_image_overlays,
    calculate_contour_framing,
    calculate_scene_adjustment,
    compose_background_through_windows,
    compose_background_through_mask,
    compose_photoroom_vehicle_with_shadow,
    compose_showroom,
    create_automatic_background_mask,
    create_openai_semantic_mask,
    create_photoroom_cutout,
    create_photoroom_shadowed_composition,
    extract_openai_magenta_mask,
    format_retry_delay,
    ImageProviderRateLimitError,
    create_photoroom_showroom,
    infer_vehicle_perspective,
    measure_vehicle_contour,
    openai_semantic_mask_prompt,
    perspective_composition_options,
    refine_manual_background_mask,
    resolve_background_composition,
    should_preserve_original_framing,
    transform_background,
    WindowCompositionResult,
    masked_background_profile,
)


def image_bytes(image: Image.Image, format_name: str) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


def test_background_mask_from_cutout_inverts_cutout_alpha() -> None:
    cutout = Image.new("RGBA", (4, 2), (240, 240, 240, 255))
    for x in (2, 3):
        for y in (0, 1):
            cutout.putpixel((x, y), (0, 0, 0, 0))

    mask = Image.open(
        io.BytesIO(background_mask_from_cutout(image_bytes(cutout, "PNG")))
    ).convert("RGBA").getchannel("A")

    assert mask.getpixel((0, 0)) == 0
    assert mask.getpixel((3, 0)) == 255


def test_background_mask_from_cutout_strengthens_translucent_glass() -> None:
    cutout = Image.new("RGBA", (4, 1), (240, 240, 240, 255))
    cutout.putpixel((1, 0), (240, 240, 240, 252))
    cutout.putpixel((2, 0), (240, 240, 240, 230))
    cutout.putpixel((3, 0), (240, 240, 240, 200))

    mask = Image.open(
        io.BytesIO(
            background_mask_from_cutout(
                image_bytes(cutout, "PNG"),
                strengthen_translucent_regions=True,
            )
        )
    ).convert("RGBA").getchannel("A")

    assert mask.getpixel((0, 0)) == 0
    assert mask.getpixel((1, 0)) == 0
    assert mask.getpixel((2, 0)) == 102
    assert mask.getpixel((3, 0)) == 255


def test_background_transform_zoom_and_offset_keep_output_filled() -> None:
    background = Image.new("RGB", (800, 600), "#164c9c")
    ImageDraw.Draw(background).rectangle((0, 0, 399, 599), fill="#e64b3c")

    transformed = Image.open(
        io.BytesIO(
            transform_background(
                image_bytes(background, "PNG"),
                width=400,
                height=300,
                zoom_percent=120,
                offset_x_percent=10,
                offset_y_percent=-10,
            )
        )
    )

    assert transformed.size == (400, 300)
    assert transformed.mode == "RGB"


def test_background_composition_uses_background_defaults() -> None:
    background = Background(
        dealership_id="00000000-0000-0000-0000-000000000001",
        name="Standard",
        object_key="background.jpg",
        content_type="image/jpeg",
        contour_target_area_percent=35,
        contour_max_width_percent=77,
        contour_max_height_percent=70,
        vehicle_bottom_percent=88,
        shadow_opacity_percent=38,
        reflection_opacity_percent=8,
        brightness_percent=102,
        background_zoom_percent=115,
        background_offset_x_percent=4,
        background_offset_y_percent=-7,
        window_background_shift_percent=16,
    )

    assert resolve_background_composition(background, None) == BackgroundComposition(
        contour_target_area_percent=35,
        contour_max_width_percent=77,
        contour_max_height_percent=70,
        vehicle_bottom_percent=88,
        shadow_opacity_percent=38,
        reflection_opacity_percent=8,
        brightness_percent=102,
        background_zoom_percent=115,
        background_offset_x_percent=4,
        background_offset_y_percent=-7,
        window_background_shift_percent=16,
    )


def test_background_composition_only_overrides_selected_orientation_values() -> None:
    background = Background(
        dealership_id="00000000-0000-0000-0000-000000000001",
        name="Standard",
        object_key="background.jpg",
        content_type="image/jpeg",
        contour_target_area_percent=36,
        contour_max_width_percent=78,
        contour_max_height_percent=72,
        vehicle_bottom_percent=90,
        shadow_opacity_percent=32,
        reflection_opacity_percent=10,
        brightness_percent=100,
        background_zoom_percent=108,
        background_offset_x_percent=0,
        background_offset_y_percent=-3,
        window_background_shift_percent=14,
    )
    override = BackgroundOrientationComposition(
        background_id="00000000-0000-0000-0000-000000000002",
        orientation_id="00000000-0000-0000-0000-000000000003",
        vehicle_bottom_percent=94,
        shadow_opacity_percent=45,
        background_zoom_percent=125,
        background_offset_y_percent=-9,
    )

    assert resolve_background_composition(background, override) == BackgroundComposition(
        contour_target_area_percent=36,
        contour_max_width_percent=78,
        contour_max_height_percent=72,
        vehicle_bottom_percent=94,
        shadow_opacity_percent=45,
        reflection_opacity_percent=10,
        brightness_percent=100,
        background_zoom_percent=125,
        background_offset_x_percent=0,
        background_offset_y_percent=-9,
        window_background_shift_percent=14,
    )


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


def test_hybrid_framing_preserves_well_composed_exterior_photo() -> None:
    frame = VehicleFrame(
        contour=VehicleContour(width=600, height=400),
        source_width=800,
        source_height=600,
        left=100,
        top=120,
        right=700,
        bottom=520,
    )
    options = CompositionOptions(
        orientation_key="front-left",
        vehicle_bottom_percent=90,
    )
    preferred = calculate_contour_framing(
        frame.contour,
        output_width=options.width,
        output_height=options.height,
    )

    assert should_preserve_original_framing(
        frame,
        options=options,
        preferred_framing=preferred,
    )


def test_hybrid_framing_corrects_vehicle_that_is_too_small() -> None:
    frame = VehicleFrame(
        contour=VehicleContour(width=240, height=160),
        source_width=800,
        source_height=600,
        left=280,
        top=300,
        right=520,
        bottom=460,
    )
    options = CompositionOptions(
        orientation_key="front-left",
        vehicle_bottom_percent=90,
    )
    preferred = calculate_contour_framing(
        frame.contour,
        output_width=options.width,
        output_height=options.height,
    )

    assert not should_preserve_original_framing(
        frame,
        options=options,
        preferred_framing=preferred,
    )


def test_hybrid_framing_corrects_vehicle_above_configured_ground_line() -> None:
    frame = VehicleFrame(
        contour=VehicleContour(width=600, height=400),
        source_width=800,
        source_height=600,
        left=100,
        top=92,
        right=700,
        bottom=492,
    )
    options = CompositionOptions(
        orientation_key="front-left",
        vehicle_bottom_percent=90,
    )
    preferred = calculate_contour_framing(
        frame.contour,
        output_width=options.width,
        output_height=options.height,
    )

    assert not should_preserve_original_framing(
        frame,
        options=options,
        preferred_framing=preferred,
    )


@pytest.mark.parametrize(
    "frame",
    [
        VehicleFrame(
            contour=VehicleContour(width=600, height=400),
            source_width=800,
            source_height=600,
            left=0,
            top=120,
            right=600,
            bottom=520,
        ),
        VehicleFrame(
            contour=VehicleContour(width=600, height=400),
            source_width=800,
            source_height=600,
            left=100,
            top=20,
            right=700,
            bottom=420,
        ),
        VehicleFrame(
            contour=VehicleContour(width=560, height=400),
            source_width=800,
            source_height=600,
            left=230,
            top=120,
            right=790,
            bottom=520,
        ),
    ],
)
def test_hybrid_framing_corrects_clipped_high_or_off_center_vehicle(
    frame: VehicleFrame,
) -> None:
    options = CompositionOptions(
        orientation_key="front-left",
        vehicle_bottom_percent=90,
    )
    preferred = calculate_contour_framing(
        frame.contour,
        output_width=options.width,
        output_height=options.height,
    )

    assert not should_preserve_original_framing(
        frame,
        options=options,
        preferred_framing=preferred,
    )


def test_hybrid_framing_only_applies_to_exterior_orientations() -> None:
    frame = VehicleFrame(
        contour=VehicleContour(width=600, height=400),
        source_width=800,
        source_height=600,
        left=100,
        top=120,
        right=700,
        bottom=520,
    )
    options = CompositionOptions(orientation_key="steering-wheel")
    preferred = calculate_contour_framing(
        frame.contour,
        output_width=options.width,
        output_height=options.height,
    )

    assert not should_preserve_original_framing(
        frame,
        options=options,
        preferred_framing=preferred,
    )


def test_local_composition_preserves_acceptable_exterior_position() -> None:
    background = image_bytes(Image.new("RGB", (800, 600), "black"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((100, 120, 699, 519), fill=(255, 0, 0, 255))

    result = compose_showroom(
        background,
        image_bytes(cutout, "PNG"),
        CompositionOptions(
            width=800,
            height=600,
            orientation_key="front-left",
            shadow_opacity_percent=0,
            reflection_opacity_percent=0,
        ),
    )

    rendered = Image.open(io.BytesIO(result)).convert("RGB")
    red_mask = Image.new("L", rendered.size)
    red_mask.putdata(
        [
            255 if red > 180 and green < 80 and blue < 80 else 0
            for red, green, blue in rendered.get_flattened_data()
        ]
    )
    assert red_mask.getbbox() == pytest.approx((100, 120, 700, 520), abs=2)


def test_local_composition_applies_manual_vehicle_scale_and_offsets() -> None:
    background = image_bytes(Image.new("RGB", (800, 600), "black"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((100, 120, 699, 519), fill=(255, 0, 0, 255))

    result = compose_showroom(
        background,
        image_bytes(cutout, "PNG"),
        CompositionOptions(
            width=800,
            height=600,
            orientation_key="front-left",
            shadow_opacity_percent=0,
            reflection_opacity_percent=0,
            vehicle_scale_percent=50,
            vehicle_offset_x_percent=10,
            vehicle_offset_y_percent=-5,
            manual_source_framing=True,
        ),
    )

    rendered = Image.open(io.BytesIO(result)).convert("RGB")
    red_mask = Image.new("L", rendered.size)
    red_mask.putdata(
        [
            255 if red > 180 and green < 80 and blue < 80 else 0
            for red, green, blue in rendered.get_flattened_data()
        ]
    )
    assert red_mask.getbbox() == pytest.approx((330, 180, 630, 380), abs=2)


def test_local_hybrid_shadow_uses_preserved_vehicle_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    background = image_bytes(Image.new("RGB", (800, 600), "black"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((100, 120, 699, 519), fill=(255, 0, 0, 255))
    shadow_position: dict[str, int] = {}

    def fake_shadow(
        alpha: Image.Image,
        canvas_size: tuple[int, int],
        *,
        x: int,
        y: int,
        **_: object,
    ) -> Image.Image:
        shadow_position.update(x=x, y=y)
        return Image.new("RGBA", canvas_size, (0, 0, 0, 0))

    monkeypatch.setattr(processing_module, "_create_vehicle_shadow", fake_shadow)

    compose_showroom(
        background,
        image_bytes(cutout, "PNG"),
        CompositionOptions(
            width=800,
            height=600,
            orientation_key="front-left",
            shadow_opacity_percent=40,
            reflection_opacity_percent=0,
        ),
    )

    assert shadow_position == {"x": 100, "y": 120}


def test_vehicle_ground_anchors_follow_each_wheel_independently() -> None:
    alpha = Image.new("L", (400, 240), 0)
    draw = ImageDraw.Draw(alpha)
    draw.rectangle((25, 25, 374, 155), fill=255)
    draw.ellipse((55, 125, 145, 195), fill=255)
    draw.ellipse((255, 135, 355, 225), fill=255)

    left_anchor, right_anchor = processing_module._vehicle_ground_anchors(alpha)

    assert 85 <= left_anchor[0] <= 125
    assert 180 <= left_anchor[1] <= 195
    assert 290 <= right_anchor[0] <= 330
    assert 210 <= right_anchor[1] <= 225
    assert right_anchor[1] - left_anchor[1] >= 20


def test_vehicle_shadow_uses_compact_layers_for_diagonal_vehicle() -> None:
    alpha = Image.new("L", (400, 240), 0)
    draw = ImageDraw.Draw(alpha)
    draw.rectangle((25, 25, 374, 155), fill=255)
    draw.ellipse((55, 125, 145, 195), fill=255)
    draw.ellipse((255, 135, 355, 225), fill=255)

    shadow = processing_module._create_vehicle_shadow(
        alpha,
        (500, 350),
        x=50,
        y=40,
        opacity_percent=70,
        perspective="diagonal",
        blur_percent=20,
        contact_percent=0,
    ).getchannel("A")

    # A soft ambient layer fills the underbody gap between the wheels.
    assert shadow.crop((190, 195, 310, 285)).getbbox() is not None

    # It remains close to the original vehicle width. In particular there
    # must be no long dark streaks or rectangular body projection.
    assert shadow.crop((0, 0, 55, 350)).getbbox() is None
    assert shadow.crop((445, 0, 500, 350)).getbbox() is None

    # Even with wheels at different heights the visible layers remain compact.
    shadow_pixels = (np.asarray(shadow) >= 10).astype(np.uint8)
    active_rows, active_columns = np.nonzero(shadow_pixels)
    assert active_columns.max() - active_columns.min() < 410
    assert active_rows.max() - active_rows.min() < 80

    # Strong pixels occupy only a narrow part of the vehicle rectangle.
    strong_pixels = np.asarray(shadow) >= 70
    assert strong_pixels.sum() < 400 * 240 * 0.08


def test_manual_editor_shadow_uses_preview_vehicle_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    background = image_bytes(Image.new("RGB", (800, 600), "black"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((100, 120, 699, 519), fill=(255, 0, 0, 255))
    shadow_position: dict[str, object] = {}

    def fake_shadow(
        alpha: Image.Image,
        canvas_size: tuple[int, int],
        *,
        x: int,
        y: int,
        distance_percent: int,
        angle_degrees: int,
        spread_percent: int,
        blur_percent: int,
        contact_percent: int,
        **_: object,
    ) -> Image.Image:
        shadow_position.update(
            x=x,
            y=y,
            alpha_size=alpha.size,
            distance_percent=distance_percent,
            angle_degrees=angle_degrees,
            spread_percent=spread_percent,
            blur_percent=blur_percent,
            contact_percent=contact_percent,
        )
        return Image.new("RGBA", canvas_size, (0, 0, 0, 0))

    monkeypatch.setattr(processing_module, "_create_vehicle_shadow", fake_shadow)

    compose_showroom(
        background,
        image_bytes(cutout, "PNG"),
        CompositionOptions(
            width=800,
            height=600,
            orientation_key="front-left",
            shadow_opacity_percent=40,
            shadow_distance_percent=7,
            shadow_angle_degrees=135,
            shadow_spread_percent=120,
            shadow_blur_percent=145,
            shadow_contact_percent=85,
            reflection_opacity_percent=0,
            vehicle_scale_percent=50,
            vehicle_offset_x_percent=10,
            vehicle_offset_y_percent=-5,
            manual_source_framing=True,
        ),
    )

    assert shadow_position == {
        "x": pytest.approx(330, abs=1),
        "y": pytest.approx(180, abs=1),
        "alpha_size": pytest.approx((300, 200), abs=1),
        "distance_percent": 7,
        "angle_degrees": 135,
        "spread_percent": 120,
        "blur_percent": 145,
        "contact_percent": 85,
    }


@pytest.mark.parametrize(
    ("step_name", "contour", "expected"),
    [
        ("Seite links", VehicleContour(1800, 700), "side"),
        ("Diagonal hinten rechts", VehicleContour(1400, 900), "diagonal"),
        ("Heck", VehicleContour(900, 1000), "straight"),
        ("Freie Perspektive", VehicleContour(1900, 700), "side"),
    ],
)
def test_vehicle_perspective_uses_step_name_with_contour_fallback(
    step_name: str,
    contour: VehicleContour,
    expected: str,
) -> None:
    assert infer_vehicle_perspective(step_name, contour) == expected


@pytest.mark.parametrize(
    ("orientation_key", "expected"),
    [
        ("front-right", "diagonal"),
        ("front-left", "diagonal"),
        ("rear-right", "diagonal"),
        ("rear-left", "diagonal"),
        ("right", "side"),
        ("left", "side"),
        ("front", "straight"),
        ("rear", "straight"),
    ],
)
def test_vehicle_perspective_prefers_orientation_key(
    orientation_key: str,
    expected: str,
) -> None:
    # Use an intentionally ambiguous name and a side-like contour to prove the
    # centrally managed orientation wins over the geometric fallback.
    assert (
        infer_vehicle_perspective(
            "Vorne rechts",
            VehicleContour(1900, 700),
            orientation_key,
        )
        == expected
    )


def test_perspective_composition_preserves_configured_ground_line() -> None:
    base = CompositionOptions(vehicle_bottom_percent=90, capture_step_name="Seite links")
    side = perspective_composition_options(base, VehicleContour(1800, 700))
    straight = perspective_composition_options(
        replace(base, capture_step_name="Heck"),
        VehicleContour(900, 1000),
    )

    assert side.vehicle_bottom_percent == 90
    assert side.contour_max_width_percent == 84
    assert straight.vehicle_bottom_percent == 90
    assert straight.contour_target_area_percent == 29
    assert straight.contour_max_width_percent == 64


def test_perspective_composition_keeps_diagonal_on_configured_ground_line() -> None:
    base = CompositionOptions(
        vehicle_bottom_percent=90,
        capture_step_name="Vorne links",
        orientation_key="front-left",
    )

    diagonal = perspective_composition_options(base, VehicleContour(1400, 900))

    assert diagonal.vehicle_bottom_percent == 90


def test_scene_adjustment_uses_pose_only_for_beta_orientations() -> None:
    enabled = CompositionOptions(
        orientation_key="front-left",
        scene_projection_enabled=True,
        scene_horizon_percent=43,
        scene_reference_vertical_degrees=0,
        scene_perspective_strength_percent=50,
        capture_metadata={
            "horizon_angle_degrees": 4.0,
            "vertical_angle_degrees": 10.0,
            "yaw_angle_degrees": 0.0,
            "field_of_view_degrees": 65.0,
            "motion_available": True,
        },
    )

    adjustment = calculate_scene_adjustment(enabled)
    unsupported = calculate_scene_adjustment(replace(enabled, orientation_key="front"))

    assert adjustment.scale_multiplier > 1
    assert adjustment.bottom_shift_fraction < 0
    assert adjustment.rotation_degrees == pytest.approx(-2)
    assert adjustment.shadow_depth_multiplier > 1
    assert unsupported.scale_multiplier == 1
    assert unsupported.bottom_shift_fraction == 0


def test_scene_adjustment_is_disabled_without_motion_metadata() -> None:
    adjustment = calculate_scene_adjustment(
        CompositionOptions(
            orientation_key="left",
            scene_projection_enabled=True,
            capture_metadata={"motion_available": False},
        )
    )

    assert adjustment == calculate_scene_adjustment(CompositionOptions())


def test_scene_adjustment_ignores_invalid_legacy_metadata() -> None:
    adjustment = calculate_scene_adjustment(
        CompositionOptions(
            orientation_key="left",
            scene_projection_enabled=True,
            capture_metadata={
                "motion_available": True,
                "vertical_angle_degrees": None,
            },
        )
    )

    assert adjustment == SceneAdjustment()


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


def test_showroom_vehicle_layer_keeps_manual_transform_on_transparent_canvas() -> None:
    background = Image.new("RGB", (400, 300), "white")
    vehicle = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    ImageDraw.Draw(vehicle).rectangle((100, 100, 299, 249), fill=(20, 30, 40, 255))

    result = compose_showroom(
        image_bytes(background, "JPEG"),
        image_bytes(vehicle, "PNG"),
        CompositionOptions(
            width=400,
            height=300,
            vehicle_scale_percent=80,
            vehicle_offset_x_percent=10,
            vehicle_offset_y_percent=-5,
            manual_source_framing=True,
        ),
        vehicle_layer_only=True,
    )

    finished = Image.open(io.BytesIO(result)).convert("RGBA")
    alpha_box = finished.getchannel("A").getbbox()
    assert finished.size == (400, 300)
    assert alpha_box is not None
    assert alpha_box == pytest.approx((160, 95, 320, 215), abs=2)
    assert finished.getpixel((20, 20))[3] == 0


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


def test_overlay_can_span_full_canvas_width_without_clipping() -> None:
    base = Image.new("RGB", (400, 300), "white")
    logo = Image.new("RGBA", (400, 100), (210, 20, 30, 255))

    result = apply_image_overlays(
        image_bytes(base, "JPEG"),
        [OverlayLayer(image_bytes(logo, "PNG"), "bottom_right", 100, 100)],
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    assert finished.size == (400, 300)
    assert finished.getpixel((2, 250))[0] > 180
    assert finished.getpixel((397, 250))[0] > 180


def test_full_canvas_overlay_preserves_transparent_coordinate_system() -> None:
    base = Image.new("RGB", (400, 300), "white")
    overlay = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle((300, 250, 399, 299), fill=(210, 20, 30, 255))

    result = apply_image_overlays(
        image_bytes(base, "JPEG"),
        [OverlayLayer(image_bytes(overlay, "PNG"), "top_left", 100, 100)],
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    assert finished.getpixel((20, 20))[0] > 240
    assert finished.getpixel((310, 260))[0] > 180
    assert finished.getpixel((310, 260))[1] < 60


def test_window_background_preserves_foreground_and_glass_transparency() -> None:
    original = Image.new("RGB", (800, 600), (230, 20, 20))
    window_mask = Image.new("RGBA", (800, 600), (255, 255, 255, 0))
    alpha = Image.new("L", window_mask.size, 255)
    draw = ImageDraw.Draw(alpha)
    draw.rectangle((0, 200, 799, 399), fill=128)
    draw.rectangle((0, 400, 799, 599), fill=0)
    window_mask.putalpha(alpha)
    background = Image.new("RGB", (800, 600), (20, 30, 230))

    result = compose_background_through_windows(
        image_bytes(original, "JPEG"),
        image_bytes(window_mask, "PNG"),
        image_bytes(background, "JPEG"),
        Settings(output_width=800, output_height=600),
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    transparent_scene = finished.getpixel((400, 80))
    tinted_glass = finished.getpixel((400, 300))
    foreground = finished.getpixel((400, 500))
    assert finished.size == (800, 600)
    assert transparent_scene[2] > 200 and transparent_scene[0] < 50
    assert tinted_glass[0] > 80 and tinted_glass[2] > 80
    assert foreground[0] > 200 and foreground[2] < 50


def test_window_background_uses_mask_inside_instrument_cluster_region() -> None:
    original = Image.new("RGB", (800, 600), (230, 220, 20))
    window_mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(window_mask).rectangle((0, 0, 799, 399), fill="white")
    background = Image.new("RGB", original.size, (20, 30, 230))

    result = compose_background_through_windows(
        image_bytes(original, "JPEG"),
        image_bytes(window_mask, "PNG"),
        image_bytes(background, "JPEG"),
        Settings(output_width=800, output_height=600),
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    replaced_glass = finished.getpixel((100, 100))
    replaced_cluster = finished.getpixel((400, 200))
    assert replaced_glass[2] > 200 and replaced_glass[0] < 50
    assert replaced_cluster[2] > 200 and replaced_cluster[0] < 50


def test_window_background_does_not_extend_mask_at_left_image_edge() -> None:
    original = Image.new("RGB", (800, 600), (230, 220, 20))
    window_mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(window_mask).rectangle((30, 0, 599, 199), fill="white")
    background = Image.new("RGB", original.size, (20, 30, 230))

    result = compose_background_through_windows(
        image_bytes(original, "JPEG"),
        image_bytes(window_mask, "PNG"),
        image_bytes(background, "JPEG"),
        Settings(output_width=800, output_height=600),
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    untouched_left_edge = finished.getpixel((20, 60))
    selected_window = finished.getpixel((100, 60))
    assert untouched_left_edge[0] > 200 and untouched_left_edge[2] < 50
    assert selected_window[2] > 200 and selected_window[0] < 50


def test_window_background_shift_reveals_lower_background_content() -> None:
    original = Image.new("RGB", (800, 600), (230, 20, 20))
    window_mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(window_mask).rectangle((100, 20, 700, 170), fill="white")
    background = Image.new("RGB", original.size, "red")
    ImageDraw.Draw(background).rectangle((0, 250, 799, 599), fill="blue")

    centered = compose_background_through_windows(
        image_bytes(original, "JPEG"),
        image_bytes(window_mask, "PNG"),
        image_bytes(background, "JPEG"),
        Settings(output_width=800, output_height=600),
        background_shift_percent=0,
    )
    shifted = compose_background_through_windows(
        image_bytes(original, "JPEG"),
        image_bytes(window_mask, "PNG"),
        image_bytes(background, "JPEG"),
        Settings(output_width=800, output_height=600),
        background_shift_percent=35,
    )

    centered_pixel = Image.open(io.BytesIO(centered)).getpixel((150, 150))
    shifted_pixel = Image.open(io.BytesIO(shifted)).getpixel((150, 150))
    assert centered_pixel[0] > centered_pixel[2]
    assert shifted_pixel[2] > shifted_pixel[0]


def test_manual_mask_refinement_snaps_rough_boundary_to_visible_edge() -> None:
    original = Image.new("RGB", (240, 140), "navy")
    ImageDraw.Draw(original).rectangle((0, 0, 119, 139), fill="silver")
    rough_mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    # The intended background starts at x=120, but the rough brush reaches
    # ten pixels into the protected silver foreground.
    ImageDraw.Draw(rough_mask).rectangle((110, 0, 239, 139), fill="white")

    refined = refine_manual_background_mask(
        image_bytes(original, "PNG"),
        image_bytes(rough_mask, "PNG"),
        boundary_radius_percent=0.06,
    )

    alpha = Image.open(io.BytesIO(refined)).convert("RGBA").getchannel("A")
    assert alpha.getpixel((105, 70)) < 32
    assert alpha.getpixel((115, 70)) < 128
    assert alpha.getpixel((125, 70)) > 128
    assert alpha.getpixel((150, 70)) > 223


def test_manual_mask_refinement_cannot_change_pixels_outside_boundary_band() -> None:
    original = Image.new("RGB", (300, 180), "black")
    ImageDraw.Draw(original).rectangle((145, 0, 299, 179), fill="white")
    rough_mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(rough_mask).rectangle((135, 0, 299, 179), fill="white")

    refined = refine_manual_background_mask(
        image_bytes(original, "PNG"),
        image_bytes(rough_mask, "PNG"),
        boundary_radius_percent=0.04,
    )

    alpha = Image.open(io.BytesIO(refined)).convert("RGBA").getchannel("A")
    assert alpha.getpixel((90, 90)) == 0
    assert alpha.getpixel((190, 90)) == 255


def test_manual_mask_refinement_limits_grabcut_working_resolution(monkeypatch) -> None:
    original = Image.new("RGB", (2400, 1600), "navy")
    rough_mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(rough_mask).rectangle((1100, 0, 2399, 1599), fill="white")
    observed_sizes: list[tuple[int, int]] = []

    def record_grabcut_size(image, *_args, **_kwargs) -> None:
        observed_sizes.append((image.shape[1], image.shape[0]))

    monkeypatch.setattr("app.processing.cv2.grabCut", record_grabcut_size)

    refined = refine_manual_background_mask(
        image_bytes(original, "JPEG"),
        image_bytes(rough_mask, "PNG"),
    )

    assert observed_sizes
    assert max(observed_sizes[0]) == 1600
    assert Image.open(io.BytesIO(refined)).size == original.size


def test_window_background_diagnostics_accept_authoritative_mask() -> None:
    original = Image.new("RGB", (800, 600), (230, 20, 20))
    window_mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(window_mask).rectangle((200, 80, 620, 280), fill="white")
    result = compose_background_through_windows(
        image_bytes(original, "JPEG"),
        image_bytes(window_mask, "PNG"),
        image_bytes(Image.new("RGB", original.size, "blue"), "JPEG"),
        Settings(output_width=800, output_height=600),
        return_diagnostics=True,
    )

    assert isinstance(result, WindowCompositionResult)
    assert result.quality_review_required is False
    assert result.quality_review_reason is None


def test_interior_mask_preserves_original_position_and_only_replaces_windows() -> None:
    original = Image.new("RGB", (800, 600), (220, 30, 30))
    mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(mask).rectangle((120, 40, 680, 190), fill="white")
    profile = masked_background_profile("front-interior", "window_background")

    result = compose_background_through_mask(
        image_bytes(original, "JPEG"),
        image_bytes(mask, "PNG"),
        image_bytes(Image.new("RGB", original.size, (20, 40, 220)), "JPEG"),
        Settings(output_width=800, output_height=600),
        profile,
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    assert finished.getpixel((400, 100))[2] > 190
    assert finished.getpixel((400, 350))[0] > 190


def test_opening_mask_can_replace_ground_without_scaling_foreground() -> None:
    original = Image.new("RGB", (800, 600), (220, 30, 30))
    mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(mask).rectangle((0, 350, 799, 599), fill="white")
    profile = masked_background_profile("driver-entry", "opening_background")

    result = compose_background_through_mask(
        image_bytes(original, "JPEG"),
        image_bytes(mask, "PNG"),
        image_bytes(Image.new("RGB", original.size, (20, 40, 220)), "JPEG"),
        Settings(output_width=800, output_height=600),
        profile,
    )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    assert finished.getpixel((400, 100))[0] > 190
    assert finished.getpixel((400, 500))[2] > 190


def test_opening_background_reports_suspiciously_small_mask() -> None:
    original = Image.new("RGB", (800, 600), "navy")
    mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    ImageDraw.Draw(mask).rectangle((350, 250, 399, 299), fill="white")
    background = Image.new("RGB", original.size, "white")
    profile = masked_background_profile("driver-entry", "opening_background")

    result = compose_background_through_mask(
        image_bytes(original, "JPEG"),
        image_bytes(mask, "PNG"),
        image_bytes(background, "JPEG"),
        Settings(output_width=800, output_height=600),
        profile,
        return_diagnostics=True,
    )

    assert isinstance(result, WindowCompositionResult)
    assert result.quality_review_required is True
    assert "Außenfläche ist ungewöhnlich klein" in result.quality_review_reason


def test_open_trunk_uses_opening_profile_with_vehicle_protection_prompt() -> None:
    profile = masked_background_profile("trunk-open", "opening_background")

    assert "trunk opening" in profile.prompt
    assert "open tailgate" in profile.negative_prompt
    assert profile.maximum_fraction > 0.80


def test_orientation_mask_prompts_override_selection_and_extend_protection() -> None:
    profile = masked_background_profile(
        "steering-wheel",
        "window_background",
        custom_prompt="only the exterior visible through the windshield",
        custom_negative_prompt="preserve the head-up display",
    )

    assert profile.prompt == "only the exterior visible through the windshield"
    assert "steering wheel" in profile.negative_prompt
    assert "preserve the head-up display" in profile.negative_prompt
    assert profile.steering_wheel_protection is True


def test_openai_semantic_mask_prompt_mattes_only_reflective_mirror_glass() -> None:
    profile = masked_background_profile("steering-wheel", "window_background")

    prompt = " ".join(openai_semantic_mask_prompt(profile).split())

    assert "interior rear-view mirror" in prompt
    assert "exterior side mirror" in prompt
    assert "matte appearance" in prompt
    assert "Never select the mirror housing, frame, mount or stalk" in prompt
    assert "never its reflective glass surface" in prompt


def test_openai_semantic_mask_prompt_uses_editable_template_placeholders() -> None:
    profile = masked_background_profile(
        "steering-wheel",
        "window_background",
        custom_prompt="select this exact exterior",
        custom_negative_prompt="preserve this exact trim",
    )

    prompt = openai_semantic_mask_prompt(
        profile,
        "MASK=[[MASKENPROMPT]]\nPROTECT=[[SCHUTZPROMPT]]",
    )

    assert "MASK=select this exact exterior" in prompt
    assert "PROTECT=" in prompt
    assert "preserve this exact trim" in prompt
    assert "[[MASKENPROMPT]]" not in prompt
    assert "[[SCHUTZPROMPT]]" not in prompt


def test_text_guided_cutout_omits_incompatible_hd_header(monkeypatch) -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    cutout = image_bytes(Image.new("RGBA", (800, 600), (20, 30, 40, 255)), "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "pr-hd-background-removal" not in request.headers
        body = request.content
        assert b'name="segmentation.prompt"' in body
        assert b"steering wheel" in body
        assert b'name="segmentation.negativePrompt"' in body
        assert b"vehicles outside the car" in body
        assert b'name="segmentation.mode"' in body
        assert b"keepSalientObject" in body
        return httpx.Response(200, content=cutout, headers={"content-type": "image/png"})

    events: list[dict] = []
    monkeypatch.setattr(
        processing_module,
        "record_external_api_usage",
        lambda context, **values: events.append({"context": context, **values}),
    )
    context = ExternalApiUsageContext(
        dealership_id=uuid.uuid4(),
        vehicle_job_id=uuid.uuid4(),
        photo_asset_id=uuid.uuid4(),
        processing_attempt=2,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_photoroom_cutout(
            original,
            Settings(photoroom_api_key="test-key"),
            segmentation_prompt="car interior including steering wheel",
            segmentation_negative_prompt="vehicles outside the car",
            segmentation_mode="keepSalientObject",
            client=client,
            usage_context=context,
        )

    assert Image.open(io.BytesIO(result)).size == (800, 600)
    assert len(events) == 1
    assert events[0]["context"] == context
    assert events[0]["provider"] == "photoroom"
    assert events[0]["operation"] == "guided_segmentation"
    assert events[0]["outcome"] == "success"


def test_photoroom_cutout_limits_large_camera_output_size() -> None:
    original = image_bytes(Image.new("RGB", (6000, 4000), "navy"), "JPEG")
    cutout = image_bytes(Image.new("RGBA", (4999, 3333), (20, 30, 40, 255)), "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        assert b'name="outputSize"' in body
        assert b"4999x3333" in body
        assert b"6000x4000" not in body
        jpeg_start = body.index(b"\xff\xd8")
        jpeg_end = body.index(b"\xff\xd9", jpeg_start) + 2
        assert Image.open(io.BytesIO(body[jpeg_start:jpeg_end])).size == (4999, 3333)
        return httpx.Response(200, content=cutout, headers={"content-type": "image/png"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_photoroom_cutout(
            original,
            Settings(photoroom_api_key="test-key"),
            client=client,
        )

    assert Image.open(io.BytesIO(result)).size == (4999, 3333)


def test_openai_magenta_overlay_ignores_original_magenta_pixels() -> None:
    original = Image.new("RGB", (800, 600), (80, 100, 130))
    ImageDraw.Draw(original).rectangle((20, 20, 180, 180), fill=(220, 20, 210))
    annotated = original.copy()
    ImageDraw.Draw(annotated).rectangle((260, 120, 680, 420), fill=(255, 0, 255))
    profile = masked_background_profile("front-interior", "window_background")

    mask_bytes = extract_openai_magenta_mask(
        image_bytes(original, "PNG"),
        image_bytes(annotated, "PNG"),
        output_size=original.size,
        profile=profile,
    )

    alpha = Image.open(io.BytesIO(mask_bytes)).getchannel("A")
    assert alpha.getpixel((100, 100)) == 0
    assert alpha.getpixel((400, 250)) == 255
    assert alpha.getpixel((750, 550)) == 0


def test_openai_semantic_mask_sends_aligned_image_edit_request(monkeypatch) -> None:
    original = Image.new("RGB", (800, 600), (90, 100, 110))
    annotated = original.copy()
    ImageDraw.Draw(annotated).rectangle((250, 120, 650, 430), fill=(255, 0, 255))
    encoded = base64.b64encode(image_bytes(annotated, "PNG")).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.openai.com/v1/images/edits"
        assert request.headers["authorization"] == "Bearer test-openai-key"
        body = request.content
        assert b'name="model"' in body
        assert b"gpt-image-2" in body
        assert b'name="prompt"' in body
        assert b"#FF00FF" in body
        assert b"A/B/C pillars" in body
        assert b'name="size"' in body
        assert b"800x592" in body
        assert b'name="output_format"' in body
        assert b"png" in body
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    events: list[dict] = []
    monkeypatch.setattr(
        processing_module,
        "record_external_api_usage",
        lambda context, **values: events.append({"context": context, **values}),
    )
    monkeypatch.setattr(
        processing_module,
        "refine_manual_background_mask",
        lambda original_bytes, mask_bytes, **kwargs: mask_bytes,
    )
    context = ExternalApiUsageContext(
        dealership_id=uuid.uuid4(),
        vehicle_job_id=uuid.uuid4(),
        photo_asset_id=uuid.uuid4(),
        processing_attempt=1,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_openai_semantic_mask(
            image_bytes(original, "JPEG"),
            Settings(openai_api_key="test-openai-key"),
            masked_background_profile("front-interior", "window_background"),
            client=client,
            usage_context=context,
        )

    assert Image.open(io.BytesIO(result)).size == original.size
    assert events == [
        {
            "context": context,
            "provider": "openai",
            "operation": "semantic_mask",
            "sandbox": False,
            "outcome": "success",
            "http_status": 200,
            "duration_ms": events[0]["duration_ms"],
            "error_message": None,
        }
    ]


def test_automatic_mask_falls_back_to_photoroom_when_openai_result_is_invalid() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "gray"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (255, 255, 255, 0))
    ImageDraw.Draw(cutout).rectangle((200, 100, 599, 499), fill="white")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "api.openai.com":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            content=image_bytes(cutout, "PNG"),
            headers={"content-type": "image/png"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result, used_openai = create_automatic_background_mask(
            original,
            Settings(
                openai_mask_enabled=True,
                openai_api_key="test-openai-key",
                photoroom_api_key="test-photoroom-key",
            ),
            masked_background_profile("front-interior", "window_background"),
            photoroom_sandbox=True,
            client=client,
        )

    assert Image.open(io.BytesIO(result)).size == (800, 600)
    assert used_openai is False
    assert requests == [
        "https://api.openai.com/v1/images/edits",
        "https://image-api.photoroom.com/v2/edit",
    ]


def test_photoroom_throttle_exposes_provider_retry_delay() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "Request was throttled. Expected available in 18429 seconds."}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImageProviderRateLimitError) as captured:
            create_photoroom_cutout(
                original,
                Settings(photoroom_api_key="test-key"),
                client=client,
            )

    assert captured.value.retry_after_seconds == 18489
    assert "5 Std. 9 Min." in str(captured.value)


def test_retry_delay_formatting() -> None:
    assert format_retry_delay(30) == "1 Min."
    assert format_retry_delay(3600) == "1 Std."
    assert format_retry_delay(3660) == "1 Std. 1 Min."


def test_window_background_rejects_empty_window_mask() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    empty_window_mask = image_bytes(
        Image.new("RGBA", (800, 600), (20, 30, 40, 0)),
        "PNG",
    )
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")

    with pytest.raises(ImageProcessingError, match="keine Scheibenfläche"):
        compose_background_through_windows(
            original,
            empty_window_mask,
            background,
            Settings(output_width=800, output_height=600),
        )


def test_window_background_rejects_mask_covering_most_of_photo() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    oversized_window_mask = image_bytes(
        Image.new("RGBA", (800, 600), (20, 30, 40, 255)),
        "PNG",
    )
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")

    with pytest.raises(ImageProcessingError, match="zu große Bildbereiche"):
        compose_background_through_windows(
            original,
            oversized_window_mask,
            background,
            Settings(output_width=800, output_height=600),
        )


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
        assert b"ai.hard" in body
        assert b'name="outputSize"' in body
        assert b"1920x1440" in body
        assert b'name="paddingLeft"' in body
        assert b"461px" in body
        assert b'name="paddingTop"' in body
        assert b"298px" in body
        assert b'name="paddingBottom"' in body
        assert b"0px" in body
        assert b'name="marginBottom"' in body
        assert b"144px" in body
        assert b'name="verticalAlignment"' in body
        assert b"bottom" in body
        assert b'name="ignorePaddingAndSnapOnCroppedSides"' in body
        assert b"false" in body
        assert b"lighting.mode" not in body
        return httpx.Response(
            200,
            content=api_result,
            headers={"content-type": "image/jpeg"},
        )

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


def test_optimized_photoroom_request_uses_perspective_framing_and_ai_shadow() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    api_result = image_bytes(Image.new("RGB", (1920, 1440), "gray"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((20, 250, 779, 349), fill=(20, 30, 40, 255))
    cutout_result = image_bytes(cutout, "PNG")
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        body = request.content
        if requests == 1:
            assert b'name="background.imageFile"' not in body
            return httpx.Response(
                200,
                content=cutout_result,
                headers={"content-type": "image/png"},
            )
        assert b'name="background.imageFile"' in body
        assert b'name="shadow.mode"' in body
        assert b"ai.hard" in body
        assert b'name="paddingLeft"' in body
        assert b'name="paddingBottom"' in body
        assert b"0px" in body
        assert b'name="marginBottom"' in body
        assert b"144px" in body
        assert b'name="verticalAlignment"' in body
        assert b"bottom" in body
        return httpx.Response(
            200,
            content=api_result,
            headers={"content-type": "image/jpeg"},
        )

    settings = Settings(photoroom_api_key="test-key", photoroom_sandbox=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_photoroom_showroom(
            original,
            background,
            "image/jpeg",
            settings,
            optimized=True,
            capture_step_name="Hinten links",
            orientation_key="rear-left",
            capture_metadata={
                "horizon_angle_degrees": 4.0,
                "vertical_angle_degrees": 10.0,
                "yaw_angle_degrees": 0.0,
                "field_of_view_degrees": 65.0,
                "motion_available": True,
            },
            scene_projection_enabled=True,
            reflection_opacity_percent=0,
            client=client,
        )

    finished = Image.open(io.BytesIO(result)).convert("RGB")
    assert finished.size == (1920, 1440)
    assert requests == 2


def test_optimized_photoroom_preserves_acceptable_exterior_framing() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    api_result = image_bytes(Image.new("RGB", (1920, 1440), "gray"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((100, 120, 699, 519), fill=(20, 30, 40, 255))
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                200,
                content=image_bytes(cutout, "PNG"),
                headers={"content-type": "image/png"},
            )
        body = request.content
        assert b'name="background.imageFile"' in body
        assert b'name="referenceBox"' in body
        assert b"originalImage" in body
        assert b'name="padding"' in body
        assert b'name="paddingLeft"' not in body
        assert b'name="verticalAlignment"' not in body
        return httpx.Response(
            200,
            content=api_result,
            headers={"content-type": "image/jpeg"},
        )

    settings = Settings(photoroom_api_key="test-key", photoroom_sandbox=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_photoroom_showroom(
            original,
            background,
            "image/jpeg",
            settings,
            optimized=True,
            orientation_key="front-left",
            client=client,
        )

    assert Image.open(io.BytesIO(result)).size == (1920, 1440)
    assert requests == 2


def test_photoroom_shadow_can_be_disabled() -> None:
    original = image_bytes(Image.new("RGB", (800, 600), "navy"), "JPEG")
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    api_result = image_bytes(Image.new("RGB", (1920, 1440), "gray"), "JPEG")
    cutout = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).rectangle((200, 100, 599, 499), fill=(20, 30, 40, 255))
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                200,
                content=image_bytes(cutout, "PNG"),
                headers={"content-type": "image/png"},
            )
        assert b'name="shadow.mode"' not in request.content
        return httpx.Response(200, content=api_result, headers={"content-type": "image/jpeg"})

    settings = Settings(photoroom_api_key="test-key", photoroom_sandbox=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        create_photoroom_showroom(
            original,
            background,
            "image/jpeg",
            settings,
            optimized=True,
            shadow_opacity_percent=0,
            client=client,
        )

    assert requests == 2


def test_photoroom_shadowed_correction_preserves_placed_canvas() -> None:
    placed_vehicle = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(placed_vehicle).rectangle(
        (260, 180, 659, 499),
        fill=(20, 30, 40, 255),
    )
    placed_bytes = image_bytes(placed_vehicle, "PNG")
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    shadowed_vehicle = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(shadowed_vehicle).rectangle(
        (250, 500, 669, 519),
        fill=(0, 0, 0, 90),
    )
    ImageDraw.Draw(shadowed_vehicle).rectangle(
        (260, 180, 659, 499),
        fill=(20, 30, 40, 255),
    )
    api_result = image_bytes(shadowed_vehicle, "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://image-api.photoroom.com/v2/edit"
        assert request.headers["x-api-key"] == "sandbox_test-key"
        assert "pr-hd-background-removal" not in request.headers
        body = request.content
        assert b'name="imageFile"' in body
        assert b"placed-vehicle.png" in body
        assert b'name="background.imageFile"' not in body
        assert b'name="removeBackground"' in body
        assert b"true" in body
        assert b'name="keepExistingAlphaChannel"' in body
        assert b"auto" in body
        assert b'name="referenceBox"' in body
        assert b"originalImage" in body
        assert b'name="background.color"' in body
        assert b"transparent" in body
        assert b'name="padding"' in body
        assert b'name="shadow.mode"' in body
        assert b"ai.hard" in body
        assert b'name="outputSize"' in body
        assert b"800x600" in body
        assert b'name="export.format"' in body
        assert b"png" in body
        return httpx.Response(
            200,
            content=api_result,
            headers={"content-type": "image/png"},
        )

    settings = Settings(
        output_width=800,
        output_height=600,
        photoroom_api_key="test-key",
        photoroom_sandbox=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_photoroom_shadowed_composition(
            placed_bytes,
            background,
            "image/jpeg",
            settings,
            shadow_opacity_percent=42,
            client=client,
        )

    finished = Image.open(io.BytesIO(result))
    assert finished.size == (800, 600)
    assert finished.format == "JPEG"
    assert finished.convert("RGB").getpixel((20, 20)) == pytest.approx(
        (255, 255, 255),
        abs=3,
    )
    assert finished.convert("RGB").getpixel((400, 300)) == pytest.approx(
        (20, 30, 40),
        abs=5,
    )
    shadow_pixel = finished.convert("RGB").getpixel((400, 510))
    assert all(145 <= value <= 185 for value in shadow_pixel)


def test_photoroom_straight_shadow_extends_downward_and_preserves_vehicle() -> None:
    placed_vehicle = Image.new("RGBA", (100, 100), (20, 30, 40, 0))
    ImageDraw.Draw(placed_vehicle).rectangle(
        (20, 10, 79, 59),
        fill=(20, 30, 40, 255),
    )
    shadowed_vehicle = placed_vehicle.copy()
    ImageDraw.Draw(shadowed_vehicle).rectangle(
        (10, 60, 89, 69),
        fill=(0, 0, 0, 120),
    )

    extended = processing_module._extend_photoroom_shadow_downward(
        shadowed_vehicle,
        placed_vehicle,
    )

    alpha = extended.getchannel("A")
    assert alpha.getpixel((15, 78)) > 0
    assert alpha.getpixel((85, 78)) > 0
    assert alpha.getpixel((5, 78)) == 0
    assert alpha.getpixel((50, 82)) > 0
    assert alpha.getpixel((50, 70)) > alpha.getpixel((50, 78))
    assert alpha.getpixel((50, 65)) >= 115
    assert alpha.getpixel((50, 10)) == 255
    assert extended.getpixel((50, 10)) == placed_vehicle.getpixel((50, 10))


def test_photoroom_shadowed_composition_accepts_exterior_360_canvas() -> None:
    placed_vehicle = Image.new("RGBA", (900, 600), (0, 0, 0, 0))
    ImageDraw.Draw(placed_vehicle).rectangle(
        (180, 150, 719, 539),
        fill=(20, 30, 40, 255),
    )
    placed_bytes = image_bytes(placed_vehicle, "PNG")
    background = image_bytes(Image.new("RGB", (900, 600), "white"), "JPEG")
    shadowed_vehicle = placed_vehicle.copy()
    ImageDraw.Draw(shadowed_vehicle).rectangle(
        (170, 540, 729, 559),
        fill=(0, 0, 0, 90),
    )
    api_result = image_bytes(shadowed_vehicle, "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b'name="outputSize"' in request.content
        assert b"900x600" in request.content
        return httpx.Response(
            200,
            content=api_result,
            headers={"content-type": "image/png"},
        )

    # The regular output is 4:3, but the exterior 360° layer is 3:2.
    settings = Settings(
        output_width=900,
        output_height=675,
        photoroom_api_key="test-key",
        photoroom_sandbox=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = create_photoroom_shadowed_composition(
            placed_bytes,
            background,
            "image/jpeg",
            settings,
            shadow_opacity_percent=42,
            client=client,
        )

    finished = Image.open(io.BytesIO(result))
    assert finished.size == (900, 600)
    assert finished.format == "JPEG"


def test_photoroom_shadowed_correction_rejects_opaque_result() -> None:
    placed_vehicle = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(placed_vehicle).rectangle(
        (260, 180, 659, 499),
        fill=(20, 30, 40, 255),
    )
    opaque_api_result = image_bytes(Image.new("RGB", (800, 600), "black"), "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=opaque_api_result,
            headers={"content-type": "image/png"},
        )

    settings = Settings(
        output_width=800,
        output_height=600,
        photoroom_api_key="test-key",
        photoroom_sandbox=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            ImageProcessingError,
            match="ohne transparenten Hintergrund",
        ):
            create_photoroom_shadowed_composition(
                image_bytes(placed_vehicle, "PNG"),
                image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG"),
                "image/jpeg",
                settings,
                shadow_opacity_percent=42,
                client=client,
            )


def test_automatic_photoroom_composition_uses_same_ai_shadow_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    vehicle = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(vehicle).rectangle((220, 180, 579, 499), fill=(20, 30, 40, 255))
    cutout = image_bytes(vehicle, "PNG")
    expected = image_bytes(Image.new("RGB", (800, 600), "#123456"), "JPEG")
    observed: dict[str, object] = {}

    def fake_shadow(
        placed_vehicle_png: bytes,
        background_bytes: bytes,
        background_content_type: str,
        settings: Settings,
        **kwargs: object,
    ) -> bytes:
        placed = Image.open(io.BytesIO(placed_vehicle_png)).convert("RGBA")
        observed["placed_size"] = placed.size
        observed["placed_alpha_bbox"] = placed.getchannel("A").getbbox()
        observed["background"] = background_bytes
        observed["content_type"] = background_content_type
        observed["operation"] = kwargs["usage_operation"]
        observed["orientation_key"] = kwargs["orientation_key"]
        return expected

    monkeypatch.setattr(
        processing_module,
        "create_photoroom_shadowed_composition",
        fake_shadow,
    )
    result = compose_photoroom_vehicle_with_shadow(
        background,
        "image/jpeg",
        cutout,
        CompositionOptions(
            width=800,
            height=600,
            shadow_opacity_percent=42,
            orientation_key="front",
        ),
        Settings(output_width=800, output_height=600),
        photoroom_sandbox=True,
    )

    assert result == expected
    assert observed["placed_size"] == (800, 600)
    assert observed["placed_alpha_bbox"] is not None
    assert observed["background"] == background
    assert observed["content_type"] == "image/jpeg"
    assert observed["operation"] == "automatic_vehicle_shadow"
    assert observed["orientation_key"] == "front"


def test_automatic_photoroom_composition_preserves_exterior_360_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    background = image_bytes(Image.new("RGB", (900, 600), "white"), "JPEG")
    vehicle = Image.new("RGBA", (900, 600), (0, 0, 0, 0))
    ImageDraw.Draw(vehicle).rectangle((75, 140, 524, 559), fill=(20, 30, 40, 255))
    cutout = image_bytes(vehicle, "PNG")
    expected = image_bytes(Image.new("RGB", (900, 600), "#123456"), "JPEG")
    observed: dict[str, object] = {}

    def fake_shadow(
        placed_vehicle_png: bytes,
        background_bytes: bytes,
        background_content_type: str,
        settings: Settings,
        **kwargs: object,
    ) -> bytes:
        placed = Image.open(io.BytesIO(placed_vehicle_png)).convert("RGBA")
        observed["placed_size"] = placed.size
        observed["placed_alpha_bbox"] = placed.getchannel("A").getbbox()
        observed["operation"] = kwargs["usage_operation"]
        return expected

    monkeypatch.setattr(
        processing_module,
        "create_photoroom_shadowed_composition",
        fake_shadow,
    )
    result = compose_photoroom_vehicle_with_shadow(
        background,
        "image/jpeg",
        cutout,
        CompositionOptions(
            width=900,
            height=600,
            shadow_opacity_percent=42,
            preserve_source_framing=True,
        ),
        Settings(output_width=900, output_height=600),
        photoroom_sandbox=True,
    )

    assert result == expected
    assert observed["placed_size"] == (900, 600)
    assert observed["placed_alpha_bbox"] == (75, 140, 525, 560)
    assert observed["operation"] == "automatic_vehicle_shadow"


def test_automatic_photoroom_shadow_failure_uses_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    background = image_bytes(Image.new("RGB", (800, 600), "white"), "JPEG")
    vehicle = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(vehicle).rectangle((220, 180, 579, 499), fill=(20, 30, 40, 255))
    cutout = image_bytes(vehicle, "PNG")
    options = CompositionOptions(width=800, height=600, shadow_opacity_percent=42)

    def unavailable(*args: object, **kwargs: object) -> bytes:
        raise ImageProcessingError("temporarily unavailable")

    monkeypatch.setattr(
        processing_module,
        "create_photoroom_shadowed_composition",
        unavailable,
    )
    result = compose_photoroom_vehicle_with_shadow(
        background,
        "image/jpeg",
        cutout,
        options,
        Settings(output_width=800, output_height=600),
        photoroom_sandbox=True,
    )

    expected = compose_showroom(background, cutout, options)
    actual_image = Image.open(io.BytesIO(result)).convert("RGB")
    expected_image = Image.open(io.BytesIO(expected)).convert("RGB")
    assert ImageChops.difference(actual_image, expected_image).getbbox() is None
