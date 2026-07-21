from __future__ import annotations

import io
import logging
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.exporting import try_enqueue_auto_export
from app.image_service import (
    get_image_settings,
    photoroom_sandbox_active,
    provider_is_available,
)
from app.models import (
    Background,
    BackgroundOrientationComposition,
    CaptureStep,
    ImageOverlay,
    JobStatus,
    Orientation,
    PhotoAsset,
    PhotoProcessingVariant,
    ProcessingStatus,
    VehicleJob,
)
from app.storage import ObjectStorage
from app.thumbnails import create_thumbnail, thumbnail_key

logger = logging.getLogger(__name__)

WINDOW_MASK_SEGMENTATION_PROMPT = "windshield, side window"


class ImageProcessingError(RuntimeError):
    """An image could not be processed into a showroom image."""


@dataclass(frozen=True)
class CompositionOptions:
    width: int = 1920
    height: int = 1440
    contour_target_area_percent: int = 36
    contour_max_width_percent: int = 78
    contour_max_height_percent: int = 72
    vehicle_bottom_percent: int = 90
    shadow_opacity_percent: int = 32
    reflection_opacity_percent: int = 10
    brightness_percent: int = 100
    capture_step_name: str = ""
    orientation_key: str = ""
    capture_metadata: dict | None = None
    scene_projection_enabled: bool = False
    scene_horizon_percent: int = 43
    scene_reference_vertical_degrees: int = 0
    scene_perspective_strength_percent: int = 35


@dataclass(frozen=True)
class OverlayLayer:
    content: bytes
    position: str = "bottom_right"
    width_percent: int = 18
    opacity_percent: int = 100


@dataclass(frozen=True)
class VehicleContour:
    width: int
    height: int


@dataclass(frozen=True)
class ContourFraming:
    width_fraction: float
    height_fraction: float


@dataclass(frozen=True)
class SceneAdjustment:
    scale_multiplier: float = 1.0
    bottom_shift_fraction: float = 0.0
    rotation_degrees: float = 0.0
    shadow_depth_multiplier: float = 1.0


@dataclass(frozen=True)
class BackgroundComposition:
    contour_target_area_percent: int
    contour_max_width_percent: int
    contour_max_height_percent: int
    vehicle_bottom_percent: int
    shadow_opacity_percent: int
    reflection_opacity_percent: int
    brightness_percent: int
    window_background_shift_percent: int


@dataclass(frozen=True)
class WindowCompositionResult:
    content: bytes
    quality_review_required: bool = False
    quality_review_reason: str | None = None


def resolve_background_composition(
    background: Background,
    override: BackgroundOrientationComposition | None,
) -> BackgroundComposition:
    """Resolve optional orientation values over the background defaults."""

    def value(name: str) -> int:
        overridden = getattr(override, name, None) if override is not None else None
        return int(overridden if overridden is not None else getattr(background, name))

    return BackgroundComposition(
        contour_target_area_percent=value("contour_target_area_percent"),
        contour_max_width_percent=value("contour_max_width_percent"),
        contour_max_height_percent=value("contour_max_height_percent"),
        vehicle_bottom_percent=value("vehicle_bottom_percent"),
        shadow_opacity_percent=value("shadow_opacity_percent"),
        reflection_opacity_percent=value("reflection_opacity_percent"),
        brightness_percent=value("brightness_percent"),
        window_background_shift_percent=value("window_background_shift_percent"),
    )


SCENE_TEST_ORIENTATIONS = frozenset({"front-left", "left", "rear-left", "rear"})


def calculate_scene_adjustment(options: CompositionOptions) -> SceneAdjustment:
    """Project capture pose onto a calibrated virtual ground plane.

    This deliberately stays subtle: a two-dimensional vehicle cutout cannot be
    re-rendered from another viewpoint, but pose-aware scale, ground contact and
    roll correction make the placement measurably more consistent.
    """
    if (
        not options.scene_projection_enabled
        or options.orientation_key not in SCENE_TEST_ORIENTATIONS
        or not options.capture_metadata
        or not options.capture_metadata.get("motion_available", False)
    ):
        return SceneAdjustment()

    metadata = options.capture_metadata
    strength = max(0.0, min(1.0, options.scene_perspective_strength_percent / 100))
    try:
        vertical = float(metadata.get("vertical_angle_degrees", 0.0))
        horizon_angle = float(metadata.get("horizon_angle_degrees", 0.0))
        field_of_view = float(metadata.get("field_of_view_degrees", 65.0))
    except (TypeError, ValueError):
        return SceneAdjustment()
    if not all(math.isfinite(value) for value in (vertical, horizon_angle, field_of_view)):
        return SceneAdjustment()
    field_of_view = max(40.0, min(100.0, field_of_view))
    pitch_delta = max(
        -15.0,
        min(15.0, vertical - options.scene_reference_vertical_degrees),
    )

    ground_depth = max(
        0.12,
        options.vehicle_bottom_percent / 100 - options.scene_horizon_percent / 100,
    )
    bottom_shift = max(
        -0.025,
        min(0.025, -(pitch_delta / 90) * ground_depth * strength),
    )
    pitch_scale = 1 + pitch_delta * 0.003 * strength
    fov_scale = math.tan(math.radians(65 / 2)) / math.tan(math.radians(field_of_view / 2))
    fov_scale = 1 + (max(0.94, min(1.06, fov_scale)) - 1) * strength
    scale_multiplier = max(0.94, min(1.06, pitch_scale * fov_scale))
    rotation = max(-3.0, min(3.0, -horizon_angle * strength))
    shadow_depth = max(0.8, min(1.25, 1 + pitch_delta * 0.012 * strength))
    return SceneAdjustment(
        scale_multiplier=scale_multiplier,
        bottom_shift_fraction=bottom_shift,
        rotation_degrees=rotation,
        shadow_depth_multiplier=shadow_depth,
    )


def infer_vehicle_perspective(
    capture_step_name: str,
    contour: VehicleContour,
    orientation_key: str = "",
) -> str:
    """Infer the broad marketing perspective without another AI request."""
    normalized_key = orientation_key.casefold().strip().replace("_", "-")
    if normalized_key in {"front-left", "front-right", "rear-left", "rear-right"}:
        return "diagonal"
    if normalized_key in {"left", "right"}:
        return "side"
    if normalized_key in {"front", "rear"}:
        return "straight"

    normalized_name = " ".join(capture_step_name.casefold().split())
    if "diagonal" in normalized_name:
        return "diagonal"
    if "seite" in normalized_name or "seitlich" in normalized_name:
        return "side"
    if normalized_name in {"front", "heck", "vorne", "hinten"}:
        return "straight"

    aspect = contour.width / max(1, contour.height)
    if aspect >= 1.8:
        return "side"
    if aspect <= 1.15:
        return "straight"
    return "diagonal"


def perspective_composition_options(
    options: CompositionOptions,
    contour: VehicleContour,
) -> CompositionOptions:
    """Adapt automatic contour framing to the photographed vehicle perspective."""
    perspective = infer_vehicle_perspective(
        options.capture_step_name,
        contour,
        options.orientation_key,
    )
    if perspective == "side":
        return replace(
            options,
            contour_target_area_percent=min(
                60, round(options.contour_target_area_percent * 1.05)
            ),
            contour_max_width_percent=min(90, options.contour_max_width_percent + 6),
            vehicle_bottom_percent=max(55, options.vehicle_bottom_percent - 8),
        )
    if perspective == "straight":
        return replace(
            options,
            contour_target_area_percent=max(
                15, round(options.contour_target_area_percent * 0.80)
            ),
            contour_max_width_percent=min(options.contour_max_width_percent, 64),
            contour_max_height_percent=min(options.contour_max_height_percent, 64),
            vehicle_bottom_percent=max(55, options.vehicle_bottom_percent - 8),
        )
    return replace(
        options,
        contour_target_area_percent=min(
            60, round(options.contour_target_area_percent * 1.10)
        ),
        contour_max_width_percent=min(90, options.contour_max_width_percent + 2),
        # Three-quarter views need more visual contact with the foreground than
        # the narrower front/rear and side profiles. Keep the configured ground
        # line instead of raising them by three percentage points.
        vehicle_bottom_percent=options.vehicle_bottom_percent,
    )


def photoroom_shadow_mode(opacity_percent: int) -> str | None:
    """Map the dealership shadow strength to Photoroom's supported modes."""
    if opacity_percent <= 0:
        return None
    if opacity_percent < 30:
        return "ai.soft"
    return "ai.hard"


def remove_vehicle_background(image: bytes, settings: Settings) -> bytes:
    if not settings.remove_bg_api_key:
        raise ImageProcessingError("Kein KI-Dienst für die Freistellung konfiguriert")
    try:
        response = httpx.post(
            "https://api.remove.bg/v1.0/removebg",
            headers={"X-Api-Key": settings.remove_bg_api_key},
            files={"image_file": ("vehicle.jpg", image, "image/jpeg")},
            data={"size": settings.remove_bg_size, "type": "car", "format": "png"},
            timeout=120,
        )
    except httpx.HTTPError as exc:
        raise ImageProcessingError("Der KI-Dienst ist nicht erreichbar") from exc
    if response.status_code != 200:
        detail = response.text.replace("\n", " ")[:300]
        raise ImageProcessingError(
            f"Freistellung fehlgeschlagen (HTTP {response.status_code}): {detail}"
        )
    if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageProcessingError("Der KI-Dienst hat kein gültiges PNG geliefert")
    return response.content


def _photoroom_api_key(settings: Settings, sandbox: bool | None = None) -> str:
    if not settings.photoroom_api_key:
        raise ImageProcessingError("Photoroom ist nicht konfiguriert")
    use_sandbox = settings.photoroom_sandbox if sandbox is None else sandbox
    if use_sandbox and not settings.photoroom_api_key.startswith("sandbox_"):
        return f"sandbox_{settings.photoroom_api_key}"
    return settings.photoroom_api_key


def measure_vehicle_contour(cutout_png_bytes: bytes) -> VehicleContour:
    """Measure the visible subject while ignoring faint antialiasing and watermarks."""
    try:
        cutout = Image.open(io.BytesIO(cutout_png_bytes)).convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Die Fahrzeugkontur konnte nicht gelesen werden") from exc
    solid_alpha = cutout.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    box = solid_alpha.getbbox()
    if box is None:
        raise ImageProcessingError("Die Freistellung enthält keine messbare Fahrzeugkontur")
    return VehicleContour(width=box[2] - box[0], height=box[3] - box[1])


def calculate_contour_framing(
    contour: VehicleContour,
    *,
    output_width: int,
    output_height: int,
    target_area_percent: int = 36,
    max_width_percent: int = 78,
    max_height_percent: int = 72,
) -> ContourFraming:
    """Calculate a consistent perceived subject area while preserving its aspect ratio."""
    if contour.width <= 0 or contour.height <= 0 or output_width <= 0 or output_height <= 0:
        raise ImageProcessingError("Die Fahrzeugkontur hat ungültige Abmessungen")
    target_area = max(0.15, min(0.60, target_area_percent / 100))
    subject_aspect = contour.width / contour.height
    canvas_aspect = output_width / output_height
    width_fraction = math.sqrt(target_area * subject_aspect / canvas_aspect)
    height_fraction = width_fraction * canvas_aspect / subject_aspect
    limit = min(
        1.0,
        max(0.40, min(0.95, max_width_percent / 100)) / width_fraction,
        max(0.40, min(0.90, max_height_percent / 100)) / height_fraction,
    )
    return ContourFraming(
        width_fraction=width_fraction * limit,
        height_fraction=height_fraction * limit,
    )


def create_photoroom_cutout(
    original_bytes: bytes,
    settings: Settings,
    photoroom_sandbox: bool | None = None,
    *,
    segmentation_prompt: str | None = None,
    segmentation_negative_prompt: str | None = None,
    segmentation_mode: str | None = None,
    client: httpx.Client | None = None,
) -> bytes:
    """Request a transparent, original-frame cutout for contour measurement."""
    try:
        original = ImageOps.exif_transpose(Image.open(io.BytesIO(original_bytes)))
        original_size = original.size
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Das Originalbild ist ungültig") from exc
    request = client.post if client is not None else httpx.post
    request_data = {
        "removeBackground": "true",
        "referenceBox": "originalImage",
        "outputSize": f"{original_size[0]}x{original_size[1]}",
        "padding": "0",
        "export.format": "png",
    }
    if segmentation_prompt:
        request_data["segmentation.prompt"] = segmentation_prompt
    if segmentation_negative_prompt:
        request_data["segmentation.negativePrompt"] = segmentation_negative_prompt
    if segmentation_mode:
        request_data["segmentation.mode"] = segmentation_mode
    headers = {"x-api-key": _photoroom_api_key(settings, photoroom_sandbox)}
    if not segmentation_prompt and not segmentation_negative_prompt:
        headers["pr-hd-background-removal"] = "auto"
    try:
        response = request(
            "https://image-api.photoroom.com/v2/edit",
            headers=headers,
            files={"imageFile": ("vehicle.jpg", original_bytes, "image/jpeg")},
            data=request_data,
            timeout=180,
        )
    except httpx.HTTPError as exc:
        raise ImageProcessingError("Photoroom ist nicht erreichbar") from exc
    if response.status_code != 200:
        detail = response.text.replace("\n", " ")[:300]
        raise ImageProcessingError(
            f"Konturerkennung fehlgeschlagen (HTTP {response.status_code}): {detail}"
        )
    try:
        result = Image.open(io.BytesIO(response.content)).convert("RGBA")
        result.load()
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Photoroom hat keine gültige Kontur geliefert") from exc
    if result.getchannel("A").getbbox() is None:
        raise ImageProcessingError("Photoroom hat kein Fahrzeug erkannt")
    output = io.BytesIO()
    result.save(output, format="PNG", optimize=True)
    return output.getvalue()


def create_photoroom_showroom(
    original_bytes: bytes,
    background_bytes: bytes,
    background_content_type: str,
    settings: Settings,
    contour_target_area_percent: int = 36,
    contour_max_width_percent: int = 78,
    contour_max_height_percent: int = 72,
    vehicle_bottom_percent: int = 90,
    shadow_opacity_percent: int = 32,
    reflection_opacity_percent: int = 10,
    brightness_percent: int = 100,
    capture_step_name: str = "",
    orientation_key: str = "",
    capture_metadata: dict | None = None,
    scene_projection_enabled: bool = False,
    scene_horizon_percent: int = 43,
    scene_reference_vertical_degrees: int = 0,
    scene_perspective_strength_percent: int = 35,
    photoroom_sandbox: bool | None = None,
    optimized: bool = False,
    *,
    client: httpx.Client | None = None,
) -> bytes:
    """Measure the contour, then let Photoroom render the final showroom result."""
    request = client.post if client is not None else httpx.post
    cutout = create_photoroom_cutout(
        original_bytes,
        settings,
        photoroom_sandbox,
        client=client,
    )
    contour = measure_vehicle_contour(cutout)
    composition_options = CompositionOptions(
        width=settings.output_width,
        height=settings.output_height,
        contour_target_area_percent=contour_target_area_percent,
        contour_max_width_percent=contour_max_width_percent,
        contour_max_height_percent=contour_max_height_percent,
        vehicle_bottom_percent=vehicle_bottom_percent,
        shadow_opacity_percent=shadow_opacity_percent,
        reflection_opacity_percent=reflection_opacity_percent,
        brightness_percent=brightness_percent,
        capture_step_name=capture_step_name,
        orientation_key=orientation_key,
        capture_metadata=capture_metadata,
        scene_projection_enabled=scene_projection_enabled,
        scene_horizon_percent=scene_horizon_percent,
        scene_reference_vertical_degrees=scene_reference_vertical_degrees,
        scene_perspective_strength_percent=scene_perspective_strength_percent,
    )
    if optimized:
        composition_options = perspective_composition_options(
            composition_options,
            contour,
        )
    # Keep the regular provider result as an unchanged A/B comparison baseline.
    scene_adjustment = (
        calculate_scene_adjustment(composition_options)
        if optimized
        else SceneAdjustment()
    )
    composition_options = replace(
        composition_options,
        contour_target_area_percent=round(
            composition_options.contour_target_area_percent
            * scene_adjustment.scale_multiplier**2
        ),
        vehicle_bottom_percent=round(
            composition_options.vehicle_bottom_percent
            + scene_adjustment.bottom_shift_fraction * 100
        ),
    )

    framing = calculate_contour_framing(
        contour,
        output_width=settings.output_width,
        output_height=settings.output_height,
        target_area_percent=composition_options.contour_target_area_percent,
        max_width_percent=composition_options.contour_max_width_percent,
        max_height_percent=composition_options.contour_max_height_percent,
    )
    horizontal_padding = max(0.02, (1 - framing.width_fraction) / 2)
    bottom_padding = max(0.02, 1 - composition_options.vehicle_bottom_percent / 100)
    top_padding = min(0.49, max(0.02, 1 - framing.height_fraction - bottom_padding))
    background_extension = "png" if background_content_type == "image/png" else "jpg"
    edit_options = {
        "removeBackground": "true",
        "background.color": "FFFFFF",
        "outputSize": f"{settings.output_width}x{settings.output_height}",
        "paddingLeft": f"{horizontal_padding:.3f}",
        "paddingRight": f"{horizontal_padding:.3f}",
        "paddingTop": f"{top_padding:.3f}",
        "paddingBottom": f"{bottom_padding:.3f}",
        "horizontalAlignment": "center",
        "verticalAlignment": "bottom",
        "export.format": "jpeg",
    }
    shadow_mode = photoroom_shadow_mode(shadow_opacity_percent)
    if shadow_mode is not None:
        # Photoroom derives tyre contact points and perspective. Its API exposes
        # discrete soft/hard modes instead of numeric opacity, so the configured
        # intensity selects the closest supported mode.
        edit_options["shadow.mode"] = shadow_mode
    try:
        response = request(
            "https://image-api.photoroom.com/v2/edit",
            headers={
                "x-api-key": _photoroom_api_key(settings, photoroom_sandbox),
                "pr-hd-background-removal": "auto",
            },
            files={
                "imageFile": ("vehicle.jpg", original_bytes, "image/jpeg"),
                "background.imageFile": (
                    f"showroom-background.{background_extension}",
                    background_bytes,
                    background_content_type,
                ),
            },
            data=edit_options,
            timeout=180,
        )
    except httpx.HTTPError as exc:
        raise ImageProcessingError("Photoroom ist nicht erreichbar") from exc
    if response.status_code != 200:
        detail = response.text.replace("\n", " ")[:300]
        raise ImageProcessingError(
            f"Photoroom-Verarbeitung fehlgeschlagen (HTTP {response.status_code}): {detail}"
        )
    try:
        finished = Image.open(io.BytesIO(response.content))
        finished.load()
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Photoroom hat kein gültiges Bild geliefert") from exc
    if finished.size != (settings.output_width, settings.output_height):
        finished = ImageOps.fit(
            finished.convert("RGB"),
            (settings.output_width, settings.output_height),
            method=Image.Resampling.LANCZOS,
        )
    output = io.BytesIO()
    finished.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


def apply_cutout_mask_to_original(original_bytes: bytes, cutout_png_bytes: bytes) -> bytes:
    """Keep original pixels while using the AI result only as transparency mask."""
    try:
        original = ImageOps.exif_transpose(Image.open(io.BytesIO(original_bytes))).convert("RGBA")
        cutout = Image.open(io.BytesIO(cutout_png_bytes)).convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ImageProcessingError(
            "Die Freistellung konnte nicht mit dem Original verbunden werden"
        ) from exc

    alpha = cutout.getchannel("A")
    if alpha.getbbox() is None:
        raise ImageProcessingError("Die Freistellung enthält kein Fahrzeug")
    if alpha.size != original.size:
        alpha = alpha.resize(original.size, Image.Resampling.LANCZOS)
    original.putalpha(alpha)
    output = io.BytesIO()
    original.save(output, format="PNG", optimize=True)
    return output.getvalue()


def compose_background_through_windows(
    original_bytes: bytes,
    window_mask_png_bytes: bytes,
    background_bytes: bytes,
    settings: Settings,
    background_shift_percent: int = 14,
    *,
    return_diagnostics: bool = False,
) -> bytes | WindowCompositionResult:
    """Replace only AI-selected glass while preserving every other original pixel."""
    try:
        original = ImageOps.exif_transpose(
            Image.open(io.BytesIO(original_bytes))
        ).convert("RGBA")
        window_mask = Image.open(io.BytesIO(window_mask_png_bytes)).convert("RGBA")
        window_alpha = window_mask.getchannel("A")
        if window_alpha.size != original.size:
            window_alpha = window_alpha.resize(original.size, Image.Resampling.LANCZOS)
        automatic_window_alpha = window_alpha.copy()

        # The open driver's door leaves a small side-window opening at the
        # upper-left edge of this guided orientation. Text-guided segmentation
        # misses it regularly because it touches the image boundary, so add a
        # conservative calibrated glass area without reaching the A-pillar.
        driver_window_alpha = Image.new("L", original.size, 0)
        ImageDraw.Draw(driver_window_alpha).polygon(
            [
                (round(original.width * x), round(original.height * y))
                for x, y in (
                    (0.0, 0.0),
                    (0.041, 0.0),
                    (0.042, 0.04),
                    (0.043, 0.10),
                    (0.045, 0.16),
                    (0.047, 0.22),
                    (0.047, 0.235),
                    (0.0, 0.235),
                )
            ],
            fill=255,
        )
        driver_window_alpha = driver_window_alpha.filter(
            ImageFilter.GaussianBlur(max(1, round(max(original.size) * 0.0008)))
        )
        window_alpha = ImageChops.lighter(window_alpha, driver_window_alpha)

        # This orientation is captured with a guided, stable composition. A
        # smooth calibrated protection zone is therefore more reliable than a
        # second semantic mask, which can fragment dark, unlit instrument
        # clusters. Only glass outside this zone may be replaced.
        protected_alpha = Image.new("L", original.size, 0)
        ImageDraw.Draw(protected_alpha).polygon(
            [
                (round(original.width * x), round(original.height * y))
                for x, y in (
                    (0.30, 0.22),
                    (0.36, 0.18),
                    (0.66, 0.18),
                    (0.74, 0.24),
                    (0.75, 0.38),
                    (0.70, 0.43),
                    (0.32, 0.43),
                    (0.28, 0.36),
                    (0.28, 0.27),
                )
            ],
            fill=255,
        )
        protected_alpha = protected_alpha.filter(
            ImageFilter.GaussianBlur(max(2, round(max(original.size) * 0.0025)))
        )

        # Keep a separate, almost hard mask for the door frame and A-pillar.
        # Combining it only after the softer cluster protection prevents the
        # two blurred masks from creating a bright mixed seam at the edge.
        a_pillar_alpha = Image.new("L", original.size, 0)
        ImageDraw.Draw(a_pillar_alpha).polygon(
            [
                (round(original.width * x), round(original.height * y))
                for x, y in (
                    (0.038, 0.0),
                    (0.060, 0.0),
                    (0.125, 0.25),
                    (0.065, 0.27),
                    (0.044, 0.235),
                    (0.043, 0.16),
                    (0.041, 0.10),
                    (0.040, 0.04),
                )
            ],
            fill=255,
        )
        a_pillar_expansion = max(3, round(max(original.size) * 0.0025))
        if a_pillar_expansion % 2 == 0:
            a_pillar_expansion += 1
        a_pillar_alpha = a_pillar_alpha.filter(
            ImageFilter.MaxFilter(a_pillar_expansion)
        ).filter(ImageFilter.GaussianBlur(1))
        protected_alpha = ImageChops.lighter(
            protected_alpha,
            a_pillar_alpha,
        )

        # The service returns alpha=255 for selected glass. Subtract protected
        # foreground before compositing so the dashboard cannot be cut away.
        protected_overlap = ImageChops.multiply(automatic_window_alpha, protected_alpha)
        protected_overlap_fraction = sum(protected_overlap.histogram()[16:]) / (
            protected_overlap.width * protected_overlap.height
        )
        replacement_alpha = ImageChops.multiply(
            window_alpha,
            ImageOps.invert(protected_alpha),
        )

        histogram = replacement_alpha.histogram()
        selected_fraction = sum(histogram[16:]) / (
            replacement_alpha.width * replacement_alpha.height
        )
        if selected_fraction < 0.02:
            raise ImageProcessingError("Photoroom hat keine Scheibenfläche erkannt")
        if selected_fraction > 0.75:
            raise ImageProcessingError(
                "Photoroom hat zu große Bildbereiche als Scheibe erkannt"
            )
        background = Image.open(io.BytesIO(background_bytes)).convert("RGBA")
    except ImageProcessingError:
        raise
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Der Scheibenhintergrund konnte nicht erzeugt werden") from exc

    output_size = (settings.output_width, settings.output_height)
    canvas = ImageOps.fit(
        background,
        output_size,
        method=Image.Resampling.LANCZOS,
    ).convert("RGBA")
    shift = max(0, min(35, background_shift_percent)) / 100
    if shift:
        scaled = canvas.resize(
            (
                max(1, round(canvas.width * (1 + shift))),
                max(1, round(canvas.height * (1 + shift))),
            ),
            Image.Resampling.LANCZOS,
        )
        canvas = scaled.crop(
            (
                (scaled.width - output_size[0]) // 2,
                scaled.height - output_size[1],
                (scaled.width - output_size[0]) // 2 + output_size[0],
                scaled.height,
            )
        )
    foreground = ImageOps.contain(
        original,
        output_size,
        method=Image.Resampling.LANCZOS,
    )
    contained_window_alpha = ImageOps.contain(
        replacement_alpha,
        output_size,
        method=Image.Resampling.LANCZOS,
    )
    foreground.putalpha(contained_window_alpha.point(lambda value: 255 - value))
    position = (
        (settings.output_width - foreground.width) // 2,
        (settings.output_height - foreground.height) // 2,
    )
    canvas.alpha_composite(foreground, position)

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
    content = output.getvalue()
    quality_reasons: list[str] = []
    if protected_overlap_fraction >= 0.008:
        quality_reasons.append(
            "Die automatische Scheibenerkennung berührt geschützte Innenraumbereiche."
        )
    if selected_fraction < 0.04:
        quality_reasons.append("Die erkannte Scheibenfläche ist ungewöhnlich klein.")
    elif selected_fraction > 0.55:
        quality_reasons.append("Die erkannte Scheibenfläche ist ungewöhnlich groß.")
    if return_diagnostics:
        return WindowCompositionResult(
            content=content,
            quality_review_required=bool(quality_reasons),
            quality_review_reason=" ".join(quality_reasons) or None,
        )
    return content


def compose_showroom(
    background_bytes: bytes,
    vehicle_png_bytes: bytes,
    options: CompositionOptions,
) -> bytes:
    try:
        background = Image.open(io.BytesIO(background_bytes)).convert("RGB")
        vehicle = Image.open(io.BytesIO(vehicle_png_bytes)).convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Ein Eingabebild ist ungültig") from exc

    canvas = ImageOps.fit(
        background,
        (options.width, options.height),
        method=Image.Resampling.LANCZOS,
    ).convert("RGBA")
    alpha_box = vehicle.getchannel("A").point(
        lambda value: 255 if value >= 128 else 0
    ).getbbox()
    if alpha_box is None:
        raise ImageProcessingError("Die Freistellung enthält kein Fahrzeug")
    vehicle = vehicle.crop(alpha_box)

    contour = VehicleContour(vehicle.width, vehicle.height)
    perspective = infer_vehicle_perspective(
        options.capture_step_name,
        contour,
        options.orientation_key,
    )
    options = perspective_composition_options(options, contour)
    scene_adjustment = calculate_scene_adjustment(options)
    if abs(scene_adjustment.rotation_degrees) >= 0.05:
        vehicle = vehicle.rotate(
            scene_adjustment.rotation_degrees,
            resample=Image.Resampling.BICUBIC,
            expand=True,
        )
        rotated_box = vehicle.getchannel("A").point(
            lambda value: 255 if value >= 128 else 0
        ).getbbox()
        if rotated_box is not None:
            vehicle = vehicle.crop(rotated_box)
            contour = VehicleContour(vehicle.width, vehicle.height)
    options = replace(
        options,
        contour_target_area_percent=round(
            options.contour_target_area_percent * scene_adjustment.scale_multiplier**2
        ),
        vehicle_bottom_percent=round(
            options.vehicle_bottom_percent + scene_adjustment.bottom_shift_fraction * 100
        ),
    )
    framing = calculate_contour_framing(
        contour,
        output_width=options.width,
        output_height=options.height,
        target_area_percent=options.contour_target_area_percent,
        max_width_percent=options.contour_max_width_percent,
        max_height_percent=options.contour_max_height_percent,
    )
    target_width = options.width * framing.width_fraction
    target_height = options.height * framing.height_fraction
    scale = min(target_width / vehicle.width, target_height / vehicle.height)
    vehicle = vehicle.resize(
        (max(1, int(vehicle.width * scale)), max(1, int(vehicle.height * scale))),
        Image.Resampling.LANCZOS,
    )
    if options.brightness_percent != 100:
        rgb = ImageEnhance.Brightness(vehicle.convert("RGB")).enhance(
            max(50, min(150, options.brightness_percent)) / 100
        )
        rgb.putalpha(vehicle.getchannel("A"))
        vehicle = rgb

    x = (options.width - vehicle.width) // 2
    bottom = int(options.height * max(55, min(98, options.vehicle_bottom_percent)) / 100)
    y = bottom - vehicle.height

    reflection_opacity = max(0, min(60, options.reflection_opacity_percent))
    if reflection_opacity:
        reflection = ImageOps.flip(vehicle)
        reflection_alpha = reflection.getchannel("A")
        gradient = ImageOps.invert(Image.linear_gradient("L")).resize(reflection.size)
        gradient = gradient.point(lambda value: value * value // 255)
        reflection_alpha = Image.composite(
            reflection_alpha,
            Image.new("L", reflection.size, 0),
            gradient,
        ).point(lambda value: value * reflection_opacity // 100)
        reflection.putalpha(reflection_alpha)
        canvas.alpha_composite(reflection, (x, bottom))

    shadow_opacity = max(0, min(80, options.shadow_opacity_percent))
    if shadow_opacity:
        canvas = Image.alpha_composite(
            canvas,
            _create_vehicle_shadow(
                vehicle.getchannel("A"),
                canvas.size,
                x=x,
                y=y,
                opacity_percent=shadow_opacity,
                perspective=perspective,
                depth_multiplier=scene_adjustment.shadow_depth_multiplier,
            ),
        )

    canvas.alpha_composite(vehicle, (x, y))
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


def _vehicle_contact_regions(alpha: Image.Image) -> list[tuple[int, int, int]]:
    """Return likely tyre contact runs as (left, right, bottom) in alpha coordinates."""
    mask = alpha.point(lambda value: 255 if value >= 128 else 0)
    width, height = mask.size
    pixels = mask.load()
    column_bottoms: list[int] = []
    for x_position in range(width):
        bottom = -1
        for y_position in range(height - 1, -1, -1):
            if pixels[x_position, y_position]:
                bottom = y_position
                break
        column_bottoms.append(bottom)

    global_bottom = max(column_bottoms, default=-1)
    if global_bottom < 0:
        return []
    tolerance = max(5, round(height * 0.045))
    allowed_gap = max(2, round(width * 0.012))
    minimum_width = max(4, round(width * 0.012))
    candidate_columns = [
        index
        for index, column_bottom in enumerate(column_bottoms)
        if column_bottom >= global_bottom - tolerance
    ]
    if not candidate_columns:
        return []

    runs: list[list[int]] = [[candidate_columns[0]]]
    for x_position in candidate_columns[1:]:
        if x_position - runs[-1][-1] <= allowed_gap:
            runs[-1].append(x_position)
        else:
            runs.append([x_position])

    regions = [
        (
            run[0],
            run[-1],
            max(column_bottoms[run[0] : run[-1] + 1]),
        )
        for run in runs
        if minimum_width <= run[-1] - run[0] + 1 <= width * 0.24
    ]
    if len(regions) > 4:
        regions = sorted(regions, key=lambda region: region[1] - region[0], reverse=True)[:4]
        regions.sort()
    return regions


def _create_vehicle_shadow(
    alpha: Image.Image,
    canvas_size: tuple[int, int],
    *,
    x: int,
    y: int,
    opacity_percent: int,
    perspective: str,
    depth_multiplier: float = 1.0,
) -> Image.Image:
    """Build a soft underbody shadow plus darker tyre contact shadows."""
    vehicle_width, vehicle_height = alpha.size
    bottom = y + vehicle_height

    broad_shadow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    broad_draw = ImageDraw.Draw(broad_shadow)
    broad_width = round(vehicle_width * 0.84)
    broad_height = max(18, round(vehicle_height * 0.085 * depth_multiplier))
    broad_left = x + (vehicle_width - broad_width) // 2
    broad_top = bottom - round(broad_height * 0.72)
    broad_draw.ellipse(
        (
            broad_left,
            broad_top,
            broad_left + broad_width,
            broad_top + broad_height,
        ),
        fill=(0, 0, 0, round(255 * opacity_percent / 100)),
    )
    broad_shadow = broad_shadow.filter(
        ImageFilter.GaussianBlur(max(10, round(broad_height * 0.55)))
    )

    contact_shadow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    contact_draw = ImageDraw.Draw(contact_shadow)
    contact_height = max(8, round(vehicle_height * 0.025))
    contact_alpha = min(230, round(255 * opacity_percent / 100 * 1.75))
    contact_regions = _vehicle_contact_regions(alpha)
    if not contact_regions:
        fallback_positions = {
            "side": (0.20, 0.80),
            "straight": (0.17, 0.83),
            "diagonal": (0.26, 0.74),
        }[perspective]
        contact_regions = [
            (
                round(vehicle_width * position),
                round(vehicle_width * position),
                vehicle_height - 1,
            )
            for position in fallback_positions
        ]
    for left, right, contact_bottom in contact_regions:
        region_width = right - left + 1
        contact_width = max(round(vehicle_width * 0.055), round(region_width * 1.45))
        center_x = x + (left + right) // 2
        center_y = y + contact_bottom
        contact_draw.ellipse(
            (
                center_x - contact_width // 2,
                center_y - contact_height // 2,
                center_x + contact_width // 2,
                center_y + contact_height // 2,
            ),
            fill=(0, 0, 0, contact_alpha),
        )
    contact_shadow = contact_shadow.filter(
        ImageFilter.GaussianBlur(max(3, round(contact_height * 0.45)))
    )
    return Image.alpha_composite(broad_shadow, contact_shadow)


def apply_image_overlays(image_bytes: bytes, layers: list[OverlayLayer]) -> bytes:
    if not layers:
        return image_bytes
    try:
        canvas = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Das optimierte Bild ist ungültig") from exc

    margin = max(24, round(min(canvas.size) * 0.025))
    for layer in layers:
        try:
            overlay = Image.open(io.BytesIO(layer.content)).convert("RGBA")
        except (OSError, ValueError) as exc:
            raise ImageProcessingError("Eine Overlay-Datei ist ungültig") from exc
        alpha_box = overlay.getchannel("A").getbbox()
        if alpha_box is None:
            raise ImageProcessingError("Ein Overlay enthält keine sichtbaren Pixel")
        overlay = overlay.crop(alpha_box)
        target_width = max(1, round(canvas.width * max(5, min(60, layer.width_percent)) / 100))
        scale = target_width / overlay.width
        target_size = (target_width, max(1, round(overlay.height * scale)))
        max_height = max(1, canvas.height - 2 * margin)
        if target_size[1] > max_height:
            height_scale = max_height / target_size[1]
            target_size = (max(1, round(target_size[0] * height_scale)), max_height)
        overlay = overlay.resize(target_size, Image.Resampling.LANCZOS)
        opacity = max(10, min(100, layer.opacity_percent))
        if opacity < 100:
            overlay.putalpha(
                overlay.getchannel("A").point(lambda value: round(value * opacity / 100))
            )

        positions = {
            "top_left": (margin, margin),
            "top_right": (canvas.width - overlay.width - margin, margin),
            "bottom_left": (margin, canvas.height - overlay.height - margin),
            "bottom_right": (
                canvas.width - overlay.width - margin,
                canvas.height - overlay.height - margin,
            ),
            "center": (
                (canvas.width - overlay.width) // 2,
                (canvas.height - overlay.height) // 2,
            ),
        }
        if layer.position not in positions:
            raise ImageProcessingError("Eine Overlay-Position ist ungültig")
        canvas.alpha_composite(overlay, positions[layer.position])

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


def _matching_overlays(db: Session, job: VehicleJob, step: CaptureStep) -> list[ImageOverlay]:
    overlays = list(
        db.scalars(
            select(ImageOverlay)
            .options(
                selectinload(ImageOverlay.locations),
                selectinload(ImageOverlay.capture_steps),
            )
            .where(
                ImageOverlay.dealership_id == job.dealership_id,
                ImageOverlay.is_active.is_(True),
            )
            .order_by(ImageOverlay.created_at, ImageOverlay.name)
        )
    )
    first_export_step_id = db.scalar(
        select(CaptureStep.id)
        .where(
            CaptureStep.dealership_id == job.dealership_id,
            CaptureStep.export_order.is_not(None),
            CaptureStep.is_active.is_(True),
        )
        .order_by(CaptureStep.export_order, CaptureStep.capture_order, CaptureStep.name)
        .limit(1)
    )
    matching: list[ImageOverlay] = []
    for overlay in overlays:
        if overlay.brand_id is not None and overlay.brand_id != job.brand_id:
            continue
        if overlay.locations and all(
            location.id != job.location_id for location in overlay.locations
        ):
            continue
        if overlay.capture_steps:
            if all(selected_step.id != step.id for selected_step in overlay.capture_steps):
                continue
        elif step.id != first_export_step_id:
            continue
        matching.append(overlay)
    return matching


def process_photo(photo_id: str) -> None:
    identifier = uuid.UUID(photo_id)
    settings = get_settings()
    storage = ObjectStorage(settings)
    completed_job_id: uuid.UUID | None = None
    try:
        with SessionLocal() as db:
            photo = db.get(PhotoAsset, identifier)
            if photo is None or photo.uploaded_at is None or not photo.is_selected:
                return
            job = db.get(VehicleJob, photo.vehicle_job_id)
            step = db.get(CaptureStep, photo.capture_step_id)
            if job is None or step is None or not step.requires_processing:
                return
            background = db.get(Background, job.background_id) if job.background_id else None
            if background is None or not background.is_active:
                raise ImageProcessingError("Für den Auftrag ist kein aktiver Hintergrund gewählt")
            image_settings = get_image_settings(db)
            orientation = db.get(Orientation, step.orientation_id) if step.orientation_id else None
            composition_override = (
                db.scalar(
                    select(BackgroundOrientationComposition).where(
                        BackgroundOrientationComposition.background_id == background.id,
                        BackgroundOrientationComposition.orientation_id == orientation.id,
                    )
                )
                if orientation is not None
                else None
            )
            composition = resolve_background_composition(background, composition_override)
            if not provider_is_available(image_settings, settings):
                raise ImageProcessingError("Der gewählte Bilddienstleister ist nicht verfügbar")

            photo.processing_status = ProcessingStatus.PROCESSING
            photo.processing_attempts += 1
            photo.processing_error = None
            photo.processing_started_at = datetime.now(timezone.utc)
            job.status = JobStatus.PROCESSING
            db.commit()

            original = storage.get_object(object_key=photo.original_object_key)
            background_image = storage.get_object(object_key=background.object_key)
            processing_mode = orientation.processing_mode if orientation else "optimized"
            if processing_mode == "window_background":
                if image_settings.provider != "photoroom":
                    raise ImageProcessingError(
                        "Der Scheibenhintergrund benötigt Photoroom"
                    )
                if photo.window_mask_is_manual and photo.window_mask_object_key:
                    window_mask = storage.get_object(
                        object_key=photo.window_mask_object_key
                    )
                else:
                    window_mask = create_photoroom_cutout(
                        original,
                        settings,
                        photoroom_sandbox_active(image_settings, settings),
                        segmentation_prompt=WINDOW_MASK_SEGMENTATION_PROMPT,
                    )
                    mask_key = (
                        f"dealerships/{job.dealership_id}/jobs/{job.id}/"
                        f"photos/{photo.id}/window-mask.png"
                    )
                    storage.put_object(
                        object_key=mask_key,
                        content=window_mask,
                        content_type="image/png",
                    )
                    photo.window_mask_object_key = mask_key
                    photo.window_mask_is_manual = False
                window_result = compose_background_through_windows(
                    original,
                    window_mask,
                    background_image,
                    settings,
                    background_shift_percent=(
                        photo.window_background_shift_percent
                        if photo.window_background_shift_percent is not None
                        else composition.window_background_shift_percent
                    ),
                    return_diagnostics=True,
                )
                assert isinstance(window_result, WindowCompositionResult)
                finished = window_result.content
                if photo.window_mask_is_manual:
                    photo.quality_review_required = False
                    photo.quality_review_reason = None
                else:
                    photo.quality_review_required = (
                        window_result.quality_review_required
                    )
                    photo.quality_review_reason = window_result.quality_review_reason
            elif image_settings.provider == "photoroom":
                finished = create_photoroom_showroom(
                    original,
                    background_image,
                    background.content_type,
                    settings,
                    contour_target_area_percent=composition.contour_target_area_percent,
                    contour_max_width_percent=composition.contour_max_width_percent,
                    contour_max_height_percent=composition.contour_max_height_percent,
                    vehicle_bottom_percent=composition.vehicle_bottom_percent,
                    shadow_opacity_percent=composition.shadow_opacity_percent,
                    reflection_opacity_percent=composition.reflection_opacity_percent,
                    brightness_percent=composition.brightness_percent,
                    capture_step_name=step.name,
                    orientation_key=orientation.key if orientation else "",
                    capture_metadata=photo.capture_metadata,
                    scene_projection_enabled=background.scene_projection_enabled,
                    scene_horizon_percent=background.scene_horizon_percent,
                    scene_reference_vertical_degrees=background.scene_reference_vertical_degrees,
                    scene_perspective_strength_percent=(
                        background.scene_perspective_strength_percent
                    ),
                    photoroom_sandbox=photoroom_sandbox_active(image_settings, settings),
                    optimized=True,
                )
            elif image_settings.provider == "remove_bg":
                ai_cutout = remove_vehicle_background(original, settings)
                cutout = apply_cutout_mask_to_original(original, ai_cutout)
                finished = compose_showroom(
                    background_image,
                    cutout,
                    CompositionOptions(
                        width=settings.output_width,
                        height=settings.output_height,
                        contour_target_area_percent=composition.contour_target_area_percent,
                        contour_max_width_percent=composition.contour_max_width_percent,
                        contour_max_height_percent=composition.contour_max_height_percent,
                        vehicle_bottom_percent=composition.vehicle_bottom_percent,
                        shadow_opacity_percent=composition.shadow_opacity_percent,
                        reflection_opacity_percent=composition.reflection_opacity_percent,
                        brightness_percent=composition.brightness_percent,
                        capture_step_name=step.name,
                        orientation_key=orientation.key if orientation else "",
                        capture_metadata=photo.capture_metadata,
                        scene_projection_enabled=background.scene_projection_enabled,
                        scene_horizon_percent=background.scene_horizon_percent,
                        scene_reference_vertical_degrees=(
                            background.scene_reference_vertical_degrees
                        ),
                        scene_perspective_strength_percent=(
                            background.scene_perspective_strength_percent
                        ),
                    ),
                )
            else:
                raise ImageProcessingError("Die Bildverarbeitung ist deaktiviert")
            matching_overlays = _matching_overlays(db, job, step)
            if matching_overlays:
                finished = apply_image_overlays(
                    finished,
                    [
                        OverlayLayer(
                            content=storage.get_object(object_key=overlay.object_key),
                            position=overlay.position,
                            width_percent=overlay.width_percent,
                            opacity_percent=overlay.opacity_percent,
                        )
                        for overlay in matching_overlays
                    ],
                )
            processed_key = (
                f"dealerships/{job.dealership_id}/jobs/{job.id}/processed/{step.id}/{photo.id}.jpg"
            )
            storage.put_object(
                object_key=processed_key,
                content=finished,
                content_type="image/jpeg",
            )
            processed_thumbnail_key = thumbnail_key(processed_key)
            storage.put_object(
                object_key=processed_thumbnail_key,
                content=create_thumbnail(finished),
                content_type="image/jpeg",
            )
            photo.processed_object_key = processed_key
            photo.processed_content_type = "image/jpeg"
            photo.processed_size_bytes = len(finished)
            photo.processed_thumbnail_object_key = processed_thumbnail_key
            photo.processed_provider = image_settings.provider
            photo.processing_status = ProcessingStatus.COMPLETED
            photo.processing_completed_at = datetime.now(timezone.utc)
            job.status = _next_job_status(db, job.id)
            db.commit()
            completed_job_id = job.id
    except Exception as exc:
        with SessionLocal() as db:
            photo = db.get(PhotoAsset, identifier)
            if photo is not None:
                photo.processing_status = ProcessingStatus.FAILED
                photo.processing_error = str(exc)[:1000]
                job = db.get(VehicleJob, photo.vehicle_job_id)
                if job is not None:
                    job.status = JobStatus.REVIEW_REQUIRED
                db.commit()
        raise
    if completed_job_id is not None:
        try:
            try_enqueue_auto_export(completed_job_id)
        except Exception:
            logger.exception("Automatic export could not be queued for job %s", completed_job_id)
            with SessionLocal() as db:
                job = db.get(VehicleJob, completed_job_id)
                if job is not None:
                    job.status = JobStatus.REVIEW_REQUIRED
                    db.commit()


def process_photo_variant(photo_id: str, provider: str) -> None:
    if provider not in {"photoroom", "photoroom_optimized"}:
        raise ImageProcessingError(f"Unbekannte Vergleichsverarbeitung: {provider}")
    identifier = uuid.UUID(photo_id)
    settings = get_settings()
    storage = ObjectStorage(settings)
    try:
        with SessionLocal() as db:
            photo = db.get(PhotoAsset, identifier)
            if photo is None or photo.uploaded_at is None or not photo.is_selected:
                return
            job = db.get(VehicleJob, photo.vehicle_job_id)
            step = db.get(CaptureStep, photo.capture_step_id)
            if job is None or step is None or not step.requires_processing:
                return
            background = db.get(Background, job.background_id) if job.background_id else None
            if background is None or not background.is_active:
                raise ImageProcessingError("Für den Auftrag ist kein aktiver Hintergrund gewählt")
            image_settings = get_image_settings(db)
            orientation = db.get(Orientation, step.orientation_id) if step.orientation_id else None
            composition_override = (
                db.scalar(
                    select(BackgroundOrientationComposition).where(
                        BackgroundOrientationComposition.background_id == background.id,
                        BackgroundOrientationComposition.orientation_id == orientation.id,
                    )
                )
                if orientation is not None
                else None
            )
            composition = resolve_background_composition(background, composition_override)

            variant = db.scalar(
                select(PhotoProcessingVariant).where(
                    PhotoProcessingVariant.photo_asset_id == photo.id,
                    PhotoProcessingVariant.provider == provider,
                )
            )
            if variant is None:
                variant = PhotoProcessingVariant(photo_asset_id=photo.id, provider=provider)
                db.add(variant)
            variant.status = ProcessingStatus.PROCESSING.value
            variant.attempts += 1
            variant.error = None
            variant.started_at = datetime.now(timezone.utc)
            db.commit()

            original = storage.get_object(object_key=photo.original_object_key)
            background_image = storage.get_object(object_key=background.object_key)
            if orientation is not None and orientation.processing_mode == "window_background":
                if photo.window_mask_is_manual and photo.window_mask_object_key:
                    window_mask = storage.get_object(
                        object_key=photo.window_mask_object_key
                    )
                else:
                    window_mask = create_photoroom_cutout(
                        original,
                        settings,
                        photoroom_sandbox_active(image_settings, settings),
                        segmentation_prompt=WINDOW_MASK_SEGMENTATION_PROMPT,
                    )
                finished = compose_background_through_windows(
                    original,
                    window_mask,
                    background_image,
                    settings,
                    background_shift_percent=(
                        photo.window_background_shift_percent
                        if photo.window_background_shift_percent is not None
                        else composition.window_background_shift_percent
                    ),
                )
            else:
                finished = create_photoroom_showroom(
                    original,
                    background_image,
                    background.content_type,
                    settings,
                    contour_target_area_percent=composition.contour_target_area_percent,
                    contour_max_width_percent=composition.contour_max_width_percent,
                    contour_max_height_percent=composition.contour_max_height_percent,
                    vehicle_bottom_percent=composition.vehicle_bottom_percent,
                    shadow_opacity_percent=composition.shadow_opacity_percent,
                    reflection_opacity_percent=composition.reflection_opacity_percent,
                    brightness_percent=composition.brightness_percent,
                    capture_step_name=step.name,
                    orientation_key=orientation.key if orientation else "",
                    capture_metadata=photo.capture_metadata,
                    scene_projection_enabled=background.scene_projection_enabled,
                    scene_horizon_percent=background.scene_horizon_percent,
                    scene_reference_vertical_degrees=(
                        background.scene_reference_vertical_degrees
                    ),
                    scene_perspective_strength_percent=(
                        background.scene_perspective_strength_percent
                    ),
                    photoroom_sandbox=photoroom_sandbox_active(image_settings, settings),
                    optimized=provider == "photoroom_optimized",
                )
            object_key = (
                f"dealerships/{job.dealership_id}/jobs/{job.id}/comparisons/"
                f"{provider}/{step.id}/{photo.id}.jpg"
            )
            storage.put_object(
                object_key=object_key,
                content=finished,
                content_type="image/jpeg",
            )
            variant_thumbnail_key = thumbnail_key(object_key)
            storage.put_object(
                object_key=variant_thumbnail_key,
                content=create_thumbnail(finished),
                content_type="image/jpeg",
            )
            variant.object_key = object_key
            variant.content_type = "image/jpeg"
            variant.size_bytes = len(finished)
            variant.thumbnail_object_key = variant_thumbnail_key
            variant.status = ProcessingStatus.COMPLETED.value
            variant.completed_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        with SessionLocal() as db:
            variant = db.scalar(
                select(PhotoProcessingVariant).where(
                    PhotoProcessingVariant.photo_asset_id == identifier,
                    PhotoProcessingVariant.provider == provider,
                )
            )
            if variant is not None:
                variant.status = ProcessingStatus.FAILED.value
                variant.error = str(exc)[:1000]
                db.commit()
        raise


def _next_job_status(db, job_id: uuid.UUID) -> JobStatus:
    statuses = set(
        db.scalars(
            select(PhotoAsset.processing_status).where(
                PhotoAsset.vehicle_job_id == job_id,
                PhotoAsset.is_selected.is_(True),
            )
        )
    )
    if statuses & {ProcessingStatus.PENDING, ProcessingStatus.QUEUED, ProcessingStatus.PROCESSING}:
        return JobStatus.PROCESSING
    return JobStatus.REVIEW_REQUIRED
