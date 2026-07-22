from __future__ import annotations

import base64
import io
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import cv2
import httpx
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.api_usage import ExternalApiUsageContext, record_external_api_usage
from app.database import SessionLocal
from app.exporting import try_enqueue_auto_export
from app.image_service import (
    get_image_settings,
    photoroom_sandbox_active,
    provider_is_available,
)
from app.orientations import MASKED_BACKGROUND_MODES
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
MASK_REFINEMENT_MAX_DIMENSION = 1600
OPENAI_MASK_MAX_DIMENSION = 1920


class ImageProcessingError(RuntimeError):
    """An image could not be processed into a showroom image."""


class ImageProviderRateLimitError(ImageProcessingError):
    """The image provider rejected work until a known later point in time."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(60, min(retry_after_seconds, 86_400))
        super().__init__(
            "Der Bilddienst ist vorübergehend limitiert. "
            f"Automatischer neuer Versuch in etwa {format_retry_delay(self.retry_after_seconds)}."
        )


def format_retry_delay(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    hours, remaining_minutes = divmod(minutes, 60)
    if hours and remaining_minutes:
        return f"{hours} Std. {remaining_minutes} Min."
    if hours:
        return f"{hours} Std."
    return f"{remaining_minutes} Min."


def raise_for_photoroom_rate_limit(response: httpx.Response) -> None:
    if response.status_code != 429:
        return
    retry_after = response.headers.get("retry-after", "").strip()
    retry_after_seconds = int(retry_after) if retry_after.isdigit() else 0
    if retry_after_seconds <= 0:
        match = re.search(
            r"Expected available in\s+(\d+)\s+seconds",
            response.text,
            flags=re.IGNORECASE,
        )
        retry_after_seconds = int(match.group(1)) if match else 3600
    # Give the provider a small buffer so that the scheduled request does not
    # arrive exactly at the edge of its rolling quota window.
    raise ImageProviderRateLimitError(retry_after_seconds + 60)


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


@dataclass(frozen=True)
class MaskedBackgroundProfile:
    prompt: str
    negative_prompt: str
    minimum_fraction: float
    maximum_fraction: float
    steering_wheel_protection: bool = False


def openai_semantic_mask_prompt(profile: MaskedBackgroundProfile) -> str:
    """Build the visual annotation prompt used for deterministic local extraction."""
    return f"""
Create a pixel-aligned annotation of this exact photograph. Preserve the original
resolution, crop, perspective and every image detail. Do not move, redraw, retouch,
brighten or replace anything.

Paint only the regions described below with a flat, fully opaque, uniform pure blue
#0000FF overlay. The blue overlay is a technical segmentation label, not a realistic
edit. Every pixel outside the selected regions must remain identical to the input.

SELECT: {profile.prompt}. Select only the exterior environment visible through glass
or through a physical vehicle opening. Include every disconnected matching region,
including small side-window and door-opening regions at an image edge.

NEVER SELECT: {profile.negative_prompt}. Also preserve all vehicle structure and
interior components, including A/B/C pillars, roof liner, dashboard, instrument
cluster, steering wheel, seats, door panels, mirrors, window seals, frames, screens,
controls and trim. Preserve reflections and glass edges; mark the view through the
glass, not the surrounding vehicle parts.

Return the annotated photograph only. Do not add text, legends, outlines or new
objects.
""".strip()


def _openai_mask_working_image(original_bytes: bytes) -> tuple[bytes, tuple[int, int]]:
    """Normalize a source photograph for an aligned, bounded-cost image edit request."""
    try:
        original = ImageOps.exif_transpose(Image.open(io.BytesIO(original_bytes))).convert("RGB")
        original.load()
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Das Originalbild ist für die KI-Maske ungültig") from exc
    scale = min(1.0, OPENAI_MASK_MAX_DIMENSION / max(original.size))
    # GPT Image accepts dimensions divisible by 16. Round down so mask
    # preparation never invents pixels or exceeds the working resolution.
    width = max(16, int(original.width * scale) // 16 * 16)
    height = max(16, int(original.height * scale) // 16 * 16)
    working = original.resize((width, height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    working.save(output, format="PNG", optimize=True)
    return output.getvalue(), original.size


def extract_openai_blue_mask(
    original_working_bytes: bytes,
    annotated_bytes: bytes,
    *,
    output_size: tuple[int, int],
    profile: MaskedBackgroundProfile,
) -> bytes:
    """Convert only newly painted saturated blue pixels into an alpha mask."""
    try:
        source = Image.open(io.BytesIO(original_working_bytes)).convert("RGB")
        annotated = Image.open(io.BytesIO(annotated_bytes)).convert("RGB")
        if annotated.size != source.size:
            annotated = annotated.resize(source.size, Image.Resampling.LANCZOS)
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("OpenAI hat kein gültiges Maskenbild geliefert") from exc

    source_array = np.asarray(source, dtype=np.int16)
    result_array = np.asarray(annotated, dtype=np.int16)
    red = result_array[:, :, 0]
    green = result_array[:, :, 1]
    blue = result_array[:, :, 2]
    changed = np.max(np.abs(result_array - source_array), axis=2) >= 18
    selected = (
        (blue >= 145)
        & ((blue - red) >= 55)
        & ((blue - green) >= 30)
        & changed
    )
    selected_u8 = selected.astype(np.uint8) * 255
    close_radius = max(3, round(max(source.size) * 0.0035))
    if close_radius % 2 == 0:
        close_radius += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_radius, close_radius))
    selected_u8 = cv2.morphologyEx(selected_u8, cv2.MORPH_CLOSE, kernel)

    # Discard isolated blue details introduced by reflections or badges while
    # retaining small, genuine edge-connected window/opening regions.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(selected_u8, connectivity=8)
    cleaned = np.zeros_like(selected_u8)
    minimum_area = max(24, round(selected_u8.size * 0.00012))
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= minimum_area:
            cleaned[labels == label] = 255

    fraction = float(np.count_nonzero(cleaned)) / cleaned.size
    if fraction < profile.minimum_fraction:
        raise ImageProcessingError("OpenAI hat keine ausreichende Maskenfläche erkannt")
    if fraction > profile.maximum_fraction:
        raise ImageProcessingError("OpenAI hat eine unplausibel große Maskenfläche erkannt")

    alpha = Image.fromarray(cleaned, mode="L")
    if alpha.size != output_size:
        alpha = alpha.resize(output_size, Image.Resampling.LANCZOS)
    mask = Image.new("RGBA", output_size, (255, 255, 255, 0))
    mask.putalpha(alpha)
    output = io.BytesIO()
    mask.save(output, format="PNG", optimize=True)
    return output.getvalue()


def create_openai_semantic_mask(
    original_bytes: bytes,
    settings: Settings,
    profile: MaskedBackgroundProfile,
    *,
    client: httpx.Client | None = None,
    usage_context: ExternalApiUsageContext | None = None,
) -> bytes:
    """Ask GPT Image for a blue semantic overlay and extract it locally as a mask."""
    if not settings.openai_api_key:
        raise ImageProcessingError("Kein OpenAI-Schlüssel für KI-Masken konfiguriert")
    working_bytes, original_size = _openai_mask_working_image(original_bytes)
    with Image.open(io.BytesIO(working_bytes)) as working:
        working_size = working.size
    request = client.post if client is not None else httpx.post
    started = time.perf_counter()
    try:
        response = request(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            files={"image": ("source.png", working_bytes, "image/png")},
            data={
                "model": settings.openai_mask_model,
                "prompt": openai_semantic_mask_prompt(profile),
                "size": f"{working_size[0]}x{working_size[1]}",
                "quality": "high",
                "output_format": "png",
                "n": "1",
            },
            timeout=settings.openai_mask_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        record_external_api_usage(
            usage_context,
            provider="openai",
            operation="semantic_mask",
            sandbox=False,
            outcome="network_error",
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_message=str(exc),
        )
        raise ImageProcessingError("OpenAI ist für die Maskenerzeugung nicht erreichbar") from exc
    record_external_api_usage(
        usage_context,
        provider="openai",
        operation="semantic_mask",
        sandbox=False,
        outcome=(
            "success"
            if response.status_code == 200
            else "throttled"
            if response.status_code == 429
            else "error"
        ),
        http_status=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000),
        error_message=None if response.status_code == 200 else response.text,
    )
    if response.status_code != 200:
        detail = response.text.replace("\n", " ")[:300]
        raise ImageProcessingError(
            f"OpenAI-Maskenerzeugung fehlgeschlagen (HTTP {response.status_code}): {detail}"
        )
    try:
        encoded = response.json()["data"][0]["b64_json"]
        annotated_bytes = base64.b64decode(encoded, validate=True)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ImageProcessingError("OpenAI hat keine auswertbare KI-Maske geliefert") from exc
    mask = extract_openai_blue_mask(
        working_bytes,
        annotated_bytes,
        output_size=original_size,
        profile=profile,
    )
    # The model supplies semantic understanding; local edge refinement snaps
    # its broad blue annotation back to the unchanged source photograph.
    return refine_manual_background_mask(
        original_bytes,
        mask,
        boundary_radius_percent=0.006,
    )


def refine_manual_background_mask(
    original_bytes: bytes,
    mask_png_bytes: bytes,
    *,
    boundary_radius_percent: float = 0.008,
) -> bytes:
    """Snap a roughly painted replacement mask to nearby visible image edges.

    The operator's mask remains authoritative away from its boundary. GrabCut
    may only change a narrow band around that boundary, so an uncertain edge
    can be cleaned up without removing remote pillars, trim or controls.
    """
    try:
        with Image.open(io.BytesIO(original_bytes)) as opened_original:
            oriented_original = ImageOps.exif_transpose(opened_original)
            original_size = oriented_original.size
            scale = min(
                1.0,
                MASK_REFINEMENT_MAX_DIMENSION / max(original_size),
            )
            working_size = (
                max(1, round(original_size[0] * scale)),
                max(1, round(original_size[1] * scale)),
            )
            if working_size != original_size:
                original = oriented_original.resize(
                    working_size, Image.Resampling.LANCZOS
                ).convert("RGB")
            else:
                original = oriented_original.convert("RGB")
        with Image.open(io.BytesIO(mask_png_bytes)) as source_mask:
            alpha = source_mask.convert("RGBA").getchannel("A")
            if alpha.size != working_size:
                alpha = alpha.resize(working_size, Image.Resampling.LANCZOS)
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Die Maskenkante konnte nicht verfeinert werden") from exc

    selected = np.asarray(alpha, dtype=np.uint8) >= 128
    if not selected.any() or selected.all():
        raise ImageProcessingError("Die Maskenkante konnte nicht verfeinert werden")

    radius = max(4, min(28, round(max(original.size) * boundary_radius_percent)))
    kernel_size = radius * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    selected_u8 = selected.astype(np.uint8)
    sure_selected = cv2.erode(selected_u8, kernel, iterations=1).astype(bool)
    possible_selected = cv2.dilate(selected_u8, kernel, iterations=1).astype(bool)
    if not sure_selected.any() or possible_selected.all():
        # Very thin or border-filling masks do not provide reliable seeds.
        # Preserve the operator input instead of guessing beyond it.
        refined_alpha = alpha
    else:
        grabcut_mask = np.full(selected.shape, cv2.GC_BGD, dtype=np.uint8)
        grabcut_mask[possible_selected] = cv2.GC_PR_BGD
        grabcut_mask[selected] = cv2.GC_PR_FGD
        grabcut_mask[sure_selected] = cv2.GC_FGD
        image_bgr = cv2.cvtColor(np.asarray(original), cv2.COLOR_RGB2BGR)
        background_model = np.zeros((1, 65), np.float64)
        foreground_model = np.zeros((1, 65), np.float64)
        try:
            cv2.grabCut(
                image_bgr,
                grabcut_mask,
                None,
                background_model,
                foreground_model,
                4,
                cv2.GC_INIT_WITH_MASK,
            )
        except cv2.error:
            # Edge assistance must never block an operator correction. If the
            # local color model cannot converge, keep the submitted mask.
            logger.warning("Manual mask edge refinement did not converge")
            refined_alpha = alpha
        else:
            refined = np.isin(grabcut_mask, (cv2.GC_FGD, cv2.GC_PR_FGD))
            # The model cannot erase the painted core or add pixels beyond the
            # narrow uncertain edge band.
            refined[sure_selected] = True
            refined[~possible_selected] = False
            refined_alpha = Image.fromarray((refined.astype(np.uint8) * 255), mode="L")
            refined_alpha = refined_alpha.filter(ImageFilter.GaussianBlur(0.8))

    if refined_alpha.size != original_size:
        refined_alpha = refined_alpha.resize(original_size, Image.Resampling.LANCZOS)
    output_mask = Image.new("RGBA", original_size, (255, 255, 255, 0))
    output_mask.putalpha(refined_alpha)
    output = io.BytesIO()
    output_mask.save(output, format="PNG", optimize=True)
    return output.getvalue()


def masked_background_profile(
    orientation_key: str, processing_mode: str
) -> MaskedBackgroundProfile:
    """Describe the semantic area that may reveal the configured showroom."""
    if orientation_key == "steering-wheel":
        return MaskedBackgroundProfile(
            prompt=WINDOW_MASK_SEGMENTATION_PROMPT,
            negative_prompt=(
                "steering wheel, dashboard, instrument cluster, A-pillar, "
                "door frame, mirror"
            ),
            minimum_fraction=0.02,
            maximum_fraction=0.75,
            steering_wheel_protection=True,
        )
    if processing_mode == "opening_background":
        prompts = {
            "trunk-open": (
                "outdoor background visible around the vehicle and through the open "
                "trunk opening"
            ),
            "driver-entry": (
                "outdoor background and ground visible through the open driver door"
            ),
            "driver-door": (
                "window glass, outdoor background and ground visible around the driver door"
            ),
            "passenger-entry": (
                "outdoor background and ground visible through the open passenger door"
            ),
            "passenger-door": (
                "window glass, outdoor background and ground visible around the passenger door"
            ),
            "driver-door-open": (
                "outdoor background and ground visible around and through the open driver door"
            ),
            "passenger-door-open": (
                "outdoor background and ground visible around and through the open passenger door"
            ),
        }
        return MaskedBackgroundProfile(
            prompt=prompts.get(
                orientation_key,
                "outdoor background visible through the open vehicle door",
            ),
            negative_prompt=(
                "vehicle body, open door, open tailgate, cargo area, seats, dashboard, "
                "pillars, trim, mirrors"
            ),
            minimum_fraction=0.004,
            maximum_fraction=0.88,
        )
    prompts = {
        "front-interior": "windshield and side window glass",
        "rear-row-driver": "side window and rear window glass",
        "rear-row-passenger": "side window and rear window glass",
        "panoramic-roof": "panoramic glass roof and window glass",
    }
    return MaskedBackgroundProfile(
        prompt=prompts.get(orientation_key, WINDOW_MASK_SEGMENTATION_PROMPT),
        negative_prompt=(
            "dashboard, seats, steering wheel, instrument cluster, pillars, door frame, "
            "mirrors, interior trim"
        ),
        minimum_fraction=0.003,
        maximum_fraction=0.68,
    )


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


def remove_vehicle_background(
    image: bytes,
    settings: Settings,
    *,
    usage_context: ExternalApiUsageContext | None = None,
) -> bytes:
    if not settings.remove_bg_api_key:
        raise ImageProcessingError("Kein KI-Dienst für die Freistellung konfiguriert")
    started = time.perf_counter()
    try:
        response = httpx.post(
            "https://api.remove.bg/v1.0/removebg",
            headers={"X-Api-Key": settings.remove_bg_api_key},
            files={"image_file": ("vehicle.jpg", image, "image/jpeg")},
            data={"size": settings.remove_bg_size, "type": "car", "format": "png"},
            timeout=120,
        )
    except httpx.HTTPError as exc:
        record_external_api_usage(
            usage_context,
            provider="remove_bg",
            operation="background_removal",
            sandbox=False,
            outcome="network_error",
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_message=str(exc),
        )
        raise ImageProcessingError("Der KI-Dienst ist nicht erreichbar") from exc
    record_external_api_usage(
        usage_context,
        provider="remove_bg",
        operation="background_removal",
        sandbox=False,
        outcome="success" if response.status_code == 200 else "error",
        http_status=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000),
        error_message=None if response.status_code == 200 else response.text,
    )
    if response.status_code != 200:
        detail = response.text.replace("\n", " ")[:300]
        raise ImageProcessingError(
            f"Freistellung fehlgeschlagen (HTTP {response.status_code}): {detail}"
        )
    if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageProcessingError("Der KI-Dienst hat kein gültiges PNG geliefert")
    return response.content


def _photoroom_api_key(settings: Settings, sandbox: bool | None = None) -> str:
    use_sandbox = settings.photoroom_sandbox if sandbox is None else sandbox
    key = settings.photoroom_key_for(sandbox=use_sandbox)
    if not key:
        environment_name = "Sandbox" if use_sandbox else "Live-Betrieb"
        raise ImageProcessingError(
            f"Photoroom ist für den {environment_name} nicht konfiguriert"
        )
    return key


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
    usage_context: ExternalApiUsageContext | None = None,
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
    sandbox_active = settings.photoroom_sandbox if photoroom_sandbox is None else photoroom_sandbox
    operation = "guided_segmentation" if segmentation_prompt else "contour_cutout"
    started = time.perf_counter()
    try:
        response = request(
            "https://image-api.photoroom.com/v2/edit",
            headers=headers,
            files={"imageFile": ("vehicle.jpg", original_bytes, "image/jpeg")},
            data=request_data,
            timeout=180,
        )
    except httpx.HTTPError as exc:
        record_external_api_usage(
            usage_context,
            provider="photoroom",
            operation=operation,
            sandbox=sandbox_active,
            outcome="network_error",
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_message=str(exc),
        )
        raise ImageProcessingError("Photoroom ist nicht erreichbar") from exc
    record_external_api_usage(
        usage_context,
        provider="photoroom",
        operation=operation,
        sandbox=sandbox_active,
        outcome=(
            "success"
            if response.status_code == 200
            else "throttled"
            if response.status_code == 429
            else "error"
        ),
        http_status=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000),
        error_message=None if response.status_code == 200 else response.text,
    )
    raise_for_photoroom_rate_limit(response)
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


def create_automatic_background_mask(
    original_bytes: bytes,
    settings: Settings,
    profile: MaskedBackgroundProfile,
    *,
    photoroom_sandbox: bool,
    client: httpx.Client | None = None,
    usage_context: ExternalApiUsageContext | None = None,
) -> tuple[bytes, bool]:
    """Prefer the semantic OpenAI mask while retaining the proven provider fallback."""
    if settings.openai_mask_enabled:
        try:
            return (
                create_openai_semantic_mask(
                    original_bytes,
                    settings,
                    profile,
                    client=client,
                    usage_context=usage_context,
                ),
                True,
            )
        except ImageProcessingError:
            logger.exception(
                "OpenAI semantic mask was rejected; falling back to Photoroom"
            )
    return (
        create_photoroom_cutout(
            original_bytes,
            settings,
            photoroom_sandbox,
            segmentation_prompt=profile.prompt,
            segmentation_negative_prompt=profile.negative_prompt,
            client=client,
            usage_context=usage_context,
        ),
        False,
    )


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
    usage_context: ExternalApiUsageContext | None = None,
) -> bytes:
    """Measure the contour, then let Photoroom render the final showroom result."""
    request = client.post if client is not None else httpx.post
    cutout = create_photoroom_cutout(
        original_bytes,
        settings,
        photoroom_sandbox,
        client=client,
        usage_context=usage_context,
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
    sandbox_active = settings.photoroom_sandbox if photoroom_sandbox is None else photoroom_sandbox
    started = time.perf_counter()
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
        record_external_api_usage(
            usage_context,
            provider="photoroom",
            operation="showroom_composition",
            sandbox=sandbox_active,
            outcome="network_error",
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_message=str(exc),
        )
        raise ImageProcessingError("Photoroom ist nicht erreichbar") from exc
    record_external_api_usage(
        usage_context,
        provider="photoroom",
        operation="showroom_composition",
        sandbox=sandbox_active,
        outcome=(
            "success"
            if response.status_code == 200
            else "throttled"
            if response.status_code == 429
            else "error"
        ),
        http_status=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000),
        error_message=None if response.status_code == 200 else response.text,
    )
    raise_for_photoroom_rate_limit(response)
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


def compose_background_through_mask(
    original_bytes: bytes,
    mask_png_bytes: bytes,
    background_bytes: bytes,
    settings: Settings,
    profile: MaskedBackgroundProfile,
    background_shift_percent: int = 14,
    *,
    return_diagnostics: bool = False,
) -> bytes | WindowCompositionResult:
    """Replace an AI-selected view outside the cabin without moving the photo."""
    try:
        original = ImageOps.exif_transpose(
            Image.open(io.BytesIO(original_bytes))
        ).convert("RGBA")
        mask = Image.open(io.BytesIO(mask_png_bytes)).convert("RGBA").getchannel("A")
        if mask.size != original.size:
            mask = mask.resize(original.size, Image.Resampling.LANCZOS)
        # A tiny feather avoids a pasted-on edge while retaining pillars, trim,
        # the opened hatch and door seals from the untouched original.
        mask = mask.filter(
            ImageFilter.GaussianBlur(max(1, round(max(original.size) * 0.00065)))
        )
        selected_fraction = sum(mask.histogram()[16:]) / (mask.width * mask.height)
        if selected_fraction < profile.minimum_fraction:
            raise ImageProcessingError("Der Bilddienst hat keine Außenfläche erkannt")
        if selected_fraction > profile.maximum_fraction:
            raise ImageProcessingError(
                "Der Bilddienst hat zu große Bildbereiche als Außenfläche erkannt"
            )
        background = Image.open(io.BytesIO(background_bytes)).convert("RGBA")
    except ImageProcessingError:
        raise
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Der maskierte Hintergrund konnte nicht erzeugt werden") from exc

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
        # Bottom anchoring deliberately reveals more facade and ground through
        # cabin windows and open doors instead of an implausible sky-only crop.
        canvas = scaled.crop(
            (
                (scaled.width - output_size[0]) // 2,
                scaled.height - output_size[1],
                (scaled.width - output_size[0]) // 2 + output_size[0],
                scaled.height,
            )
        )

    foreground = ImageOps.contain(original, output_size, method=Image.Resampling.LANCZOS)
    contained_mask = ImageOps.contain(mask, output_size, method=Image.Resampling.LANCZOS)
    foreground.putalpha(contained_mask.point(lambda value: 255 - value))
    position = (
        (settings.output_width - foreground.width) // 2,
        (settings.output_height - foreground.height) // 2,
    )
    canvas.alpha_composite(foreground, position)

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
    content = output.getvalue()
    quality_reasons: list[str] = []
    if selected_fraction < max(profile.minimum_fraction * 2, 0.012):
        quality_reasons.append("Die erkannte Außenfläche ist ungewöhnlich klein.")
    elif selected_fraction > min(profile.maximum_fraction * 0.82, 0.70):
        quality_reasons.append("Die erkannte Außenfläche ist ungewöhnlich groß.")
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
            usage_context = ExternalApiUsageContext(
                dealership_id=job.dealership_id,
                vehicle_job_id=job.id,
                photo_asset_id=photo.id,
                processing_attempt=photo.processing_attempts,
            )

            original = storage.get_object(object_key=photo.original_object_key)
            background_image = storage.get_object(object_key=background.object_key)
            processing_mode = orientation.processing_mode if orientation else "optimized"
            if processing_mode in MASKED_BACKGROUND_MODES:
                if image_settings.provider != "photoroom":
                    raise ImageProcessingError(
                        "Die maskierte Hintergrundverarbeitung benötigt Photoroom"
                    )
                profile = masked_background_profile(
                    orientation.key if orientation else "", processing_mode
                )
                used_openai_mask = False
                if photo.window_mask_is_manual and photo.window_mask_object_key:
                    window_mask = storage.get_object(
                        object_key=photo.window_mask_object_key
                    )
                    if photo.window_mask_refine_edges:
                        try:
                            window_mask = refine_manual_background_mask(
                                original,
                                window_mask,
                            )
                        except ImageProcessingError:
                            # Edge assistance must never discard a correction.
                            # The operator mask remains the authoritative fallback.
                            logger.exception(
                                "Manual mask edge refinement failed for photo %s",
                                photo.id,
                            )
                        else:
                            refined_mask_key = (
                                f"dealerships/{job.dealership_id}/jobs/{job.id}/"
                                f"photos/{photo.id}/window-mask-manual-"
                                f"a{photo.processing_attempts}.png"
                            )
                            storage.put_object(
                                object_key=refined_mask_key,
                                content=window_mask,
                                content_type="image/png",
                            )
                            photo.window_mask_object_key = refined_mask_key
                        finally:
                            photo.window_mask_refine_edges = False
                            # Persist the one-shot refinement before the remaining
                            # composition work. A later failure must not repeat an
                            # expensive full-resolution GrabCut pass.
                            db.commit()
                else:
                    window_mask, used_openai_mask = create_automatic_background_mask(
                        original,
                        settings,
                        profile,
                        photoroom_sandbox=photoroom_sandbox_active(
                            image_settings, settings
                        ),
                        usage_context=usage_context,
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
                compose_mask = (
                    compose_background_through_windows
                    if profile.steering_wheel_protection
                    else compose_background_through_mask
                )
                compose_kwargs = {
                    "background_shift_percent": (
                        photo.window_background_shift_percent
                        if photo.window_background_shift_percent is not None
                        else composition.window_background_shift_percent
                    ),
                    "return_diagnostics": True,
                }
                if not profile.steering_wheel_protection:
                    compose_kwargs["profile"] = profile
                window_result = compose_mask(
                    original,
                    window_mask,
                    background_image,
                    settings,
                    **compose_kwargs,
                )
                assert isinstance(window_result, WindowCompositionResult)
                finished = window_result.content
                if photo.window_mask_is_manual:
                    photo.quality_review_required = True
                    photo.quality_review_reason = (
                        "Das nachbearbeitete Ergebnis wartet auf die manuelle "
                        "Operator-Freigabe."
                    )
                    photo.quality_score = 100
                    photo.quality_issues = []
                    photo.quality_model_version = "masked-background-rules-v2"
                    if photo.quality_review_created_at is None:
                        photo.quality_review_created_at = datetime.now(timezone.utc)
                    photo.quality_reviewed_by_id = None
                    photo.quality_reviewed_at = None
                    photo.quality_review_resolution = "awaiting_operator_approval"
                else:
                    was_waiting_for_review = photo.quality_review_required
                    photo.quality_review_required = bool(
                        window_result.quality_review_required
                        or (used_openai_mask and settings.openai_mask_review_all)
                    )
                    photo.quality_review_reason = (
                        window_result.quality_review_reason
                        or (
                            "Die neue KI-Maske wartet während der Qualitätserprobung "
                            "auf die Operator-Freigabe."
                            if used_openai_mask and settings.openai_mask_review_all
                            else None
                        )
                    )
                    photo.quality_score = 55 if photo.quality_review_required else 100
                    photo.quality_issues = (
                        [photo.quality_review_reason]
                        if photo.quality_review_reason
                        else []
                    )
                    photo.quality_model_version = (
                        "openai-semantic-mask-pilot-v1"
                        if used_openai_mask
                        else "masked-background-rules-v2"
                    )
                    if photo.quality_review_required:
                        if not was_waiting_for_review:
                            photo.quality_review_created_at = datetime.now(timezone.utc)
                        photo.quality_reviewed_by_id = None
                        photo.quality_reviewed_at = None
                        photo.quality_review_resolution = None
                    else:
                        photo.quality_review_resolution = "automatic_pass"
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
                    usage_context=usage_context,
                )
            elif image_settings.provider == "remove_bg":
                ai_cutout = remove_vehicle_background(
                    original, settings, usage_context=usage_context
                )
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
            # A new key per processing attempt prevents browsers and object
            # storage/CDN caches from showing an older correction after the
            # same photo was processed again.
            processed_key = (
                f"dealerships/{job.dealership_id}/jobs/{job.id}/processed/"
                f"{step.id}/{photo.id}-a{photo.processing_attempts}.jpg"
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
    except ImageProviderRateLimitError as exc:
        retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=exc.retry_after_seconds
        )
        scheduled = False
        with SessionLocal() as db:
            photo = db.get(PhotoAsset, identifier)
            if photo is not None:
                photo.processing_status = ProcessingStatus.QUEUED
                photo.processing_error = (
                    f"{exc} Frühester neuer Versuch: "
                    f"{retry_at.astimezone().strftime('%d.%m.%Y %H:%M Uhr')}."
                )[:1000]
                job = db.get(VehicleJob, photo.vehicle_job_id)
                if job is not None:
                    job.status = JobStatus.PROCESSING
                db.commit()
        try:
            from app.processing_queue import enqueue_photo_processing_at

            enqueue_photo_processing_at(identifier, retry_at)
            scheduled = True
        except Exception:
            logger.exception(
                "Rate-limited photo %s could not be scheduled for %s",
                identifier,
                retry_at,
            )
        if not scheduled:
            with SessionLocal() as db:
                photo = db.get(PhotoAsset, identifier)
                if photo is not None:
                    photo.processing_status = ProcessingStatus.FAILED
                    photo.processing_error = (
                        "Der Bilddienst ist vorübergehend limitiert. "
                        "Der automatische spätere Versuch konnte nicht eingeplant werden."
                    )
                    job = db.get(VehicleJob, photo.vehicle_job_id)
                    if job is not None:
                        job.status = JobStatus.REVIEW_REQUIRED
                    db.commit()
        return
    except ImageProcessingError as exc:
        is_masked_background_review = False
        with SessionLocal() as db:
            photo = db.get(PhotoAsset, identifier)
            if photo is not None:
                step = db.get(CaptureStep, photo.capture_step_id)
                orientation = (
                    db.get(Orientation, step.orientation_id)
                    if step is not None and step.orientation_id is not None
                    else None
                )
                is_masked_background_review = bool(
                    orientation is not None
                    and orientation.processing_mode in MASKED_BACKGROUND_MODES
                )
                photo.processing_status = ProcessingStatus.FAILED
                photo.processing_error = str(exc)[:1000]
                if is_masked_background_review:
                    photo.quality_review_required = True
                    photo.quality_review_reason = (
                        "Die automatische Scheiben- oder Öffnungserkennung konnte kein "
                        "sicheres Ergebnis "
                        f"erzeugen: {exc}"
                    )[:1000]
                    photo.quality_score = 20
                    photo.quality_issues = [str(exc)[:500]]
                    photo.quality_model_version = "masked-background-rules-v2"
                    photo.quality_review_created_at = datetime.now(timezone.utc)
                    photo.quality_reviewed_by_id = None
                    photo.quality_reviewed_at = None
                    photo.quality_review_resolution = None
                job = db.get(VehicleJob, photo.vehicle_job_id)
                if job is not None:
                    job.status = JobStatus.REVIEW_REQUIRED
                db.commit()
        if is_masked_background_review:
            return
        raise
    except Exception as exc:
        with SessionLocal() as db:
            photo = db.get(PhotoAsset, identifier)
            if photo is not None:
                photo.processing_status = ProcessingStatus.FAILED
                photo.processing_error = str(exc)[:1000]
                if not photo.quality_review_required:
                    photo.quality_review_created_at = datetime.now(timezone.utc)
                photo.quality_review_required = True
                photo.quality_review_reason = (
                    "Die automatische Bildverarbeitung ist wiederholt fehlgeschlagen: "
                    f"{exc}"
                )[:1000]
                photo.quality_score = 0
                photo.quality_issues = [str(exc)[:500]]
                photo.quality_model_version = "processing-health-v1"
                photo.quality_reviewed_by_id = None
                photo.quality_reviewed_at = None
                photo.quality_review_resolution = None
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
            usage_context = ExternalApiUsageContext(
                dealership_id=job.dealership_id,
                vehicle_job_id=job.id,
                photo_asset_id=photo.id,
                processing_attempt=variant.attempts,
            )

            original = storage.get_object(object_key=photo.original_object_key)
            background_image = storage.get_object(object_key=background.object_key)
            if (
                orientation is not None
                and orientation.processing_mode in MASKED_BACKGROUND_MODES
            ):
                profile = masked_background_profile(
                    orientation.key, orientation.processing_mode
                )
                if photo.window_mask_is_manual and photo.window_mask_object_key:
                    window_mask = storage.get_object(
                        object_key=photo.window_mask_object_key
                    )
                else:
                    window_mask, _ = create_automatic_background_mask(
                        original,
                        settings,
                        profile,
                        photoroom_sandbox=photoroom_sandbox_active(
                            image_settings, settings
                        ),
                        usage_context=usage_context,
                    )
                compose_mask = (
                    compose_background_through_windows
                    if profile.steering_wheel_protection
                    else compose_background_through_mask
                )
                compose_kwargs = {
                    "background_shift_percent": (
                        photo.window_background_shift_percent
                        if photo.window_background_shift_percent is not None
                        else composition.window_background_shift_percent
                    )
                }
                if not profile.steering_wheel_protection:
                    compose_kwargs["profile"] = profile
                finished = compose_mask(
                    original,
                    window_mask,
                    background_image,
                    settings,
                    **compose_kwargs,
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
                    usage_context=usage_context,
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
