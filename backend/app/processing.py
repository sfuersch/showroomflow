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
from app.orientations import MASKED_BACKGROUND_MODES, mask_prompt_defaults
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
    shadow_distance_percent: int = 0
    shadow_angle_degrees: int = 90
    shadow_spread_percent: int = 100
    shadow_blur_percent: int = 100
    shadow_contact_percent: int = 100
    reflection_opacity_percent: int = 10
    brightness_percent: int = 100
    background_zoom_percent: int = 100
    background_offset_x_percent: int = 0
    background_offset_y_percent: int = 0
    capture_step_name: str = ""
    orientation_key: str = ""
    capture_metadata: dict | None = None
    scene_projection_enabled: bool = False
    scene_horizon_percent: int = 43
    scene_reference_vertical_degrees: int = 0
    scene_perspective_strength_percent: int = 35
    vehicle_scale_percent: int = 100
    vehicle_offset_x_percent: int = 0
    vehicle_offset_y_percent: int = 0
    manual_source_framing: bool = False
    preserve_source_framing: bool = False


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
class VehicleFrame:
    contour: VehicleContour
    source_width: int
    source_height: int
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width_fraction(self) -> float:
        return self.contour.width / self.source_width

    @property
    def height_fraction(self) -> float:
        return self.contour.height / self.source_height

    @property
    def area_fraction(self) -> float:
        return self.width_fraction * self.height_fraction

    @property
    def center_x_fraction(self) -> float:
        return (self.left + self.right) / 2 / self.source_width

    @property
    def bottom_fraction(self) -> float:
        return self.bottom / self.source_height


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
    background_zoom_percent: int
    background_offset_x_percent: int
    background_offset_y_percent: int
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


OPENAI_MASK_PROMPT_TOKEN = "[[MASKENPROMPT]]"
OPENAI_MASK_PROTECTION_TOKEN = "[[SCHUTZPROMPT]]"
DEFAULT_OPENAI_MASK_PROMPT_TEMPLATE = f"""
Create a pixel-aligned annotation of this exact photograph. Preserve the original
resolution, crop, perspective and every image detail. Do not move, redraw, retouch,
brighten or replace anything.

Paint only the regions described below with a flat, fully opaque, uniform pure magenta
#FF00FF overlay. The magenta overlay is a technical segmentation label, not a realistic
edit. Every pixel outside the selected regions must remain identical to the input.

SELECT: {OPENAI_MASK_PROMPT_TOKEN}. Select only the exterior environment visible through glass
or through a physical vehicle opening. Include every disconnected matching region,
including small side-window and door-opening regions at an image edge.

ALSO SELECT the reflective glass surfaces of every visible interior rear-view mirror
and exterior side mirror. Cover these reflective surfaces completely with the same
pure magenta so they will be neutralized with a matte appearance. Never select the
mirror housing, frame, mount or stalk. If the protection text below mentions a mirror,
that protection applies only to its housing, frame, mount and stalk, never its
reflective glass surface.

NEVER SELECT: {OPENAI_MASK_PROTECTION_TOKEN}. Also preserve all vehicle structure and
interior components, including A/B/C pillars, roof liner, dashboard, instrument
cluster, steering wheel, seats, door panels, mirror housings, mirror frames, mirror
mounts, mirror stalks, window seals, frames, screens, controls and trim. Preserve
reflections and glass edges outside the selected mirror glass; mark the view through
the glass, not the surrounding vehicle parts.

Return the annotated photograph only. Do not add text, legends, outlines or new
objects.
""".strip()


def openai_semantic_mask_prompt(
    profile: MaskedBackgroundProfile,
    template: str | None = None,
) -> str:
    """Build the visual annotation prompt from the editable system template."""
    selected_template = (template or "").strip() or DEFAULT_OPENAI_MASK_PROMPT_TEMPLATE
    return selected_template.replace(
        OPENAI_MASK_PROMPT_TOKEN,
        profile.prompt,
    ).replace(
        OPENAI_MASK_PROTECTION_TOKEN,
        profile.negative_prompt,
    )


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


def extract_openai_magenta_mask(
    original_working_bytes: bytes,
    annotated_bytes: bytes,
    *,
    output_size: tuple[int, int],
    profile: MaskedBackgroundProfile,
) -> bytes:
    """Convert only newly painted saturated magenta pixels into an alpha mask."""
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
        (red >= 145)
        & (blue >= 145)
        & ((red - green) >= 55)
        & ((blue - green) >= 55)
        & changed
    )
    selected_u8 = selected.astype(np.uint8) * 255
    close_radius = max(3, round(max(source.size) * 0.0035))
    if close_radius % 2 == 0:
        close_radius += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_radius, close_radius))
    selected_u8 = cv2.morphologyEx(selected_u8, cv2.MORPH_CLOSE, kernel)

    # Discard isolated magenta details introduced by reflections or badges while
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
    prompt_template: str | None = None,
    client: httpx.Client | None = None,
    usage_context: ExternalApiUsageContext | None = None,
) -> bytes:
    """Ask GPT Image for a magenta semantic overlay and extract it locally as a mask."""
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
                "prompt": openai_semantic_mask_prompt(profile, prompt_template),
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
    mask = extract_openai_magenta_mask(
        working_bytes,
        annotated_bytes,
        output_size=original_size,
        profile=profile,
    )
    # The model supplies semantic understanding; local edge refinement snaps
    # its broad magenta annotation back to the unchanged source photograph.
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
    orientation_key: str,
    processing_mode: str,
    *,
    custom_prompt: str | None = None,
    custom_negative_prompt: str | None = None,
) -> MaskedBackgroundProfile:
    """Describe the semantic area that may reveal the configured showroom."""
    default_prompt, default_negative_prompt = mask_prompt_defaults(
        orientation_key, processing_mode
    )
    if orientation_key == "steering-wheel":
        profile = MaskedBackgroundProfile(
            prompt=default_prompt,
            negative_prompt=default_negative_prompt,
            minimum_fraction=0.02,
            maximum_fraction=0.75,
            steering_wheel_protection=True,
        )
    elif processing_mode == "opening_background":
        profile = MaskedBackgroundProfile(
            prompt=default_prompt,
            negative_prompt=default_negative_prompt,
            minimum_fraction=0.004,
            maximum_fraction=0.88,
        )
    else:
        profile = MaskedBackgroundProfile(
            prompt=default_prompt,
            negative_prompt=default_negative_prompt,
            minimum_fraction=0.003,
            maximum_fraction=0.68,
        )
    return replace(
        profile,
        prompt=(
            custom_prompt.strip()
            if custom_prompt and custom_prompt.strip()
            else profile.prompt
        ),
        negative_prompt=(
            f"{profile.negative_prompt}, {custom_negative_prompt.strip()}"
            if custom_negative_prompt and custom_negative_prompt.strip()
            else profile.negative_prompt
        ),
    )


def resolve_background_composition(
    background: Background,
    override: BackgroundOrientationComposition | None,
) -> BackgroundComposition:
    """Resolve optional orientation values over the background defaults."""

    def value(name: str) -> int:
        overridden = getattr(override, name, None) if override is not None else None
        base = getattr(background, name, None)
        fallback = {
            "background_zoom_percent": 100,
            "background_offset_x_percent": 0,
            "background_offset_y_percent": 0,
        }.get(name)
        return int(
            overridden
            if overridden is not None
            else base
            if base is not None
            else fallback
        )

    return BackgroundComposition(
        contour_target_area_percent=value("contour_target_area_percent"),
        contour_max_width_percent=value("contour_max_width_percent"),
        contour_max_height_percent=value("contour_max_height_percent"),
        vehicle_bottom_percent=value("vehicle_bottom_percent"),
        shadow_opacity_percent=value("shadow_opacity_percent"),
        reflection_opacity_percent=value("reflection_opacity_percent"),
        brightness_percent=value("brightness_percent"),
        background_zoom_percent=value("background_zoom_percent"),
        background_offset_x_percent=value("background_offset_x_percent"),
        background_offset_y_percent=value("background_offset_y_percent"),
        window_background_shift_percent=value("window_background_shift_percent"),
    )


def transform_background(
    background_bytes: bytes,
    *,
    width: int,
    height: int,
    zoom_percent: int = 100,
    offset_x_percent: int = 0,
    offset_y_percent: int = 0,
) -> bytes:
    """Crop and position a background without exposing empty canvas edges."""
    try:
        source = Image.open(io.BytesIO(background_bytes)).convert("RGB")
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Das Hintergrundbild ist ungültig") from exc

    base = ImageOps.fit(source, (width, height), method=Image.Resampling.LANCZOS)
    zoom = max(100, min(160, zoom_percent)) / 100
    scaled = base.resize(
        (max(width, round(width * zoom)), max(height, round(height * zoom))),
        Image.Resampling.LANCZOS,
    )
    overflow_x = scaled.width - width
    overflow_y = scaled.height - height
    requested_x = round(width * max(-25, min(25, offset_x_percent)) / 100)
    requested_y = round(height * max(-25, min(25, offset_y_percent)) / 100)
    left = max(0, min(overflow_x, overflow_x // 2 - requested_x))
    top = max(0, min(overflow_y, overflow_y // 2 - requested_y))
    transformed = scaled.crop((left, top, left + width, top + height))
    output = io.BytesIO()
    transformed.save(output, format="JPEG", quality=94, optimize=True)
    return output.getvalue()


SCENE_TEST_ORIENTATIONS = frozenset({"front-left", "left", "rear-left", "rear"})
EXTERIOR_ORIENTATIONS = frozenset(
    {
        "front",
        "front-left",
        "front-right",
        "left",
        "rear",
        "rear-left",
        "rear-right",
        "right",
    }
)


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
        )
    if perspective == "straight":
        return replace(
            options,
            contour_target_area_percent=max(
                15, round(options.contour_target_area_percent * 0.80)
            ),
            contour_max_width_percent=min(options.contour_max_width_percent, 64),
            contour_max_height_percent=min(options.contour_max_height_percent, 64),
        )
    return replace(
        options,
        contour_target_area_percent=min(
            60, round(options.contour_target_area_percent * 1.10)
        ),
        contour_max_width_percent=min(90, options.contour_max_width_percent + 2),
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
    return measure_vehicle_frame(cutout_png_bytes).contour


def measure_vehicle_frame(cutout_png_bytes: bytes) -> VehicleFrame:
    """Measure the subject and retain its position in the original image frame."""
    try:
        cutout = Image.open(io.BytesIO(cutout_png_bytes)).convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Die Fahrzeugkontur konnte nicht gelesen werden") from exc
    solid_alpha = cutout.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    box = solid_alpha.getbbox()
    if box is None:
        raise ImageProcessingError("Die Freistellung enthält keine messbare Fahrzeugkontur")
    return VehicleFrame(
        contour=VehicleContour(width=box[2] - box[0], height=box[3] - box[1]),
        source_width=cutout.width,
        source_height=cutout.height,
        left=box[0],
        top=box[1],
        right=box[2],
        bottom=box[3],
    )


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


def should_preserve_original_framing(
    frame: VehicleFrame,
    *,
    options: CompositionOptions,
    preferred_framing: ContourFraming,
) -> bool:
    """Keep a guided exterior photo unless its framing is clearly unsuitable."""
    orientation_key = options.orientation_key.casefold().strip().replace("_", "-")
    if orientation_key not in EXTERIOR_ORIENTATIONS:
        return False

    source_aspect = frame.source_width / max(1, frame.source_height)
    output_aspect = options.width / max(1, options.height)
    if abs(source_aspect / output_aspect - 1) > 0.05:
        return False

    preferred_area = preferred_framing.width_fraction * preferred_framing.height_fraction
    area_ratio = frame.area_fraction / max(0.01, preferred_area)
    max_width = max(0.40, min(0.95, options.contour_max_width_percent / 100))
    max_height = max(0.40, min(0.90, options.contour_max_height_percent / 100))
    target_bottom = max(0.55, min(0.98, options.vehicle_bottom_percent / 100))

    return (
        0.55 <= area_ratio <= 1.55
        and frame.width_fraction <= max_width + 0.06
        and frame.height_fraction <= max_height + 0.06
        and abs(frame.center_x_fraction - 0.5) <= 0.12
        # The hybrid workflow may retain a naturally composed photo, but the
        # configured ground line remains authoritative. A broad tolerance here
        # made visibly floating vehicles bypass the normal placement path.
        and abs(frame.bottom_fraction - target_bottom) <= 0.04
        and frame.left / frame.source_width >= 0.005
        and frame.right / frame.source_width <= 0.995
        and frame.top / frame.source_height >= 0.005
        and frame.bottom_fraction <= 0.995
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
    # Photoroom rejects output dimensions of 5000 pixels or more. Camera
    # originals and older 360° captures can exceed that limit, so request the
    # largest supported, aspect-ratio-preserving mask. Callers that need the
    # source resolution apply this mask back to the original afterwards.
    max_photoroom_dimension = 4999
    scale = min(
        1.0,
        max_photoroom_dimension / max(1, original_size[0]),
        max_photoroom_dimension / max(1, original_size[1]),
    )
    photoroom_size = (
        max(1, min(max_photoroom_dimension, round(original_size[0] * scale))),
        max(1, min(max_photoroom_dimension, round(original_size[1] * scale))),
    )
    request_image_bytes = original_bytes
    if photoroom_size != original_size:
        resized = original.resize(photoroom_size, Image.Resampling.LANCZOS).convert("RGB")
        resized_output = io.BytesIO()
        resized.save(resized_output, format="JPEG", quality=94, optimize=True)
        request_image_bytes = resized_output.getvalue()
    request = client.post if client is not None else httpx.post
    request_data = {
        "removeBackground": "true",
        "referenceBox": "originalImage",
        "outputSize": f"{photoroom_size[0]}x{photoroom_size[1]}",
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
            files={"imageFile": ("vehicle.jpg", request_image_bytes, "image/jpeg")},
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
    prompt_template: str | None = None,
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
                    prompt_template=prompt_template,
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
    cutout_bytes: bytes | None = None,
    *,
    client: httpx.Client | None = None,
    usage_context: ExternalApiUsageContext | None = None,
) -> bytes:
    """Measure the contour, then let Photoroom render the final showroom result."""
    request = client.post if client is not None else httpx.post
    cutout = cutout_bytes or create_photoroom_cutout(
        original_bytes,
        settings,
        photoroom_sandbox,
        client=client,
        usage_context=usage_context,
    )
    frame = measure_vehicle_frame(cutout)
    contour = frame.contour
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
    )

    framing = calculate_contour_framing(
        contour,
        output_width=settings.output_width,
        output_height=settings.output_height,
        target_area_percent=composition_options.contour_target_area_percent,
        max_width_percent=composition_options.contour_max_width_percent,
        max_height_percent=composition_options.contour_max_height_percent,
    )
    preserve_original_framing = should_preserve_original_framing(
        frame,
        options=composition_options,
        preferred_framing=framing,
    )
    background_extension = "png" if background_content_type == "image/png" else "jpg"
    edit_options = {
        "removeBackground": "true",
        "background.color": "FFFFFF",
        "outputSize": f"{settings.output_width}x{settings.output_height}",
        "export.format": "jpeg",
    }
    if preserve_original_framing:
        edit_options.update(
            {
                "referenceBox": "originalImage",
                "padding": "0",
            }
        )
    else:
        horizontal_padding = max(0.02, (1 - framing.width_fraction) / 2)
        bottom_padding = max(
            0.02,
            1 - composition_options.vehicle_bottom_percent / 100,
        )
        top_padding = min(
            0.49,
            max(0.02, 1 - framing.height_fraction - bottom_padding),
        )
        horizontal_padding_pixels = round(settings.output_width * horizontal_padding)
        top_padding_pixels = round(settings.output_height * top_padding)
        bottom_margin_pixels = round(settings.output_height * bottom_padding)
        edit_options.update(
            {
                "paddingLeft": f"{horizontal_padding_pixels}px",
                "paddingRight": f"{horizontal_padding_pixels}px",
                "paddingTop": f"{top_padding_pixels}px",
                "paddingBottom": "0px",
                # Photoroom may ignore padding when its segmentation considers
                # an edge of a large subject cropped. A margin is always
                # respected, so it makes the configured tyre contact line
                # authoritative even for high vans and transporters.
                "marginBottom": f"{bottom_margin_pixels}px",
                "horizontalAlignment": "center",
                "verticalAlignment": "bottom",
                "scaling": "fit",
                "ignorePaddingAndSnapOnCroppedSides": "false",
            }
        )
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


def create_photoroom_shadowed_composition(
    placed_vehicle_png: bytes,
    background_bytes: bytes,
    background_content_type: str,
    settings: Settings,
    *,
    shadow_opacity_percent: int,
    photoroom_sandbox: bool | None = None,
    client: httpx.Client | None = None,
    usage_context: ExternalApiUsageContext | None = None,
    usage_operation: str = "quality_correction_shadow",
) -> bytes:
    """Add an AI shadow without changing the final vehicle transform.

    ``placed_vehicle_png`` already has the final output dimensions and contains
    the vehicle at its final coordinates. Keeping the existing alpha
    channel and using the original image as reference prevents Photoroom from
    fitting or centering the subject again.
    """
    try:
        placed_vehicle = Image.open(io.BytesIO(placed_vehicle_png)).convert("RGBA")
        placed_vehicle.load()
    except (OSError, ValueError) as exc:
        raise ImageProcessingError(
            "Die korrigierte Fahrzeugebene ist ungültig"
        ) from exc
    expected_size = (settings.output_width, settings.output_height)
    if placed_vehicle.size != expected_size:
        raise ImageProcessingError(
            "Die korrigierte Fahrzeugebene hat nicht das erwartete Ausgabeformat"
        )
    if placed_vehicle.getchannel("A").getbbox() is None:
        raise ImageProcessingError(
            "Die korrigierte Fahrzeugebene enthält kein Fahrzeug"
        )

    shadow_mode = photoroom_shadow_mode(shadow_opacity_percent)
    if shadow_mode is None:
        raise ImageProcessingError(
            "Für den KI-Hauptschatten ist keine Schattenintensität eingestellt"
        )

    request_data = {
        # ``referenceBox`` is not supported when background removal is
        # disabled. Existing transparency remains authoritative through
        # ``keepExistingAlphaChannel=auto`` while Photoroom adds the shadow.
        "removeBackground": "true",
        "keepExistingAlphaChannel": "auto",
        "referenceBox": "originalImage",
        "background.color": "transparent",
        "outputSize": f"{settings.output_width}x{settings.output_height}",
        "padding": "0",
        "shadow.mode": shadow_mode,
        "export.format": "png",
    }
    request = client.post if client is not None else httpx.post
    sandbox_active = (
        settings.photoroom_sandbox
        if photoroom_sandbox is None
        else photoroom_sandbox
    )
    started = time.perf_counter()
    try:
        response = request(
            "https://image-api.photoroom.com/v2/edit",
            headers={"x-api-key": _photoroom_api_key(settings, photoroom_sandbox)},
            files={
                "imageFile": (
                    "placed-vehicle.png",
                    placed_vehicle_png,
                    "image/png",
                ),
            },
            data=request_data,
            timeout=180,
        )
    except httpx.HTTPError as exc:
        record_external_api_usage(
            usage_context,
            provider="photoroom",
            operation=usage_operation,
            sandbox=sandbox_active,
            outcome="network_error",
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_message=str(exc),
        )
        raise ImageProcessingError(
            "Photoroom ist für den KI-Hauptschatten nicht erreichbar"
        ) from exc
    record_external_api_usage(
        usage_context,
        provider="photoroom",
        operation=usage_operation,
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
            "KI-Hauptschatten fehlgeschlagen "
            f"(HTTP {response.status_code}): {detail}"
        )
    try:
        shadowed_vehicle = Image.open(io.BytesIO(response.content)).convert("RGBA")
        shadowed_vehicle.load()
    except (OSError, ValueError) as exc:
        raise ImageProcessingError(
            "Photoroom hat kein gültiges korrigiertes Bild geliefert"
        ) from exc
    if shadowed_vehicle.size != expected_size:
        raise ImageProcessingError(
            "Photoroom hat die korrigierte Fahrzeugplatzierung verändert"
        )
    shadowed_alpha = shadowed_vehicle.getchannel("A")
    if shadowed_alpha.getextrema()[0] == 255:
        raise ImageProcessingError(
            "Photoroom hat den KI-Schatten ohne transparenten Hintergrund geliefert"
        )
    try:
        background = ImageOps.fit(
            Image.open(io.BytesIO(background_bytes)).convert("RGB"),
            expected_size,
            method=Image.Resampling.LANCZOS,
        ).convert("RGBA")
        background.load()
    except (OSError, ValueError) as exc:
        raise ImageProcessingError(
            "Der Showroom-Hintergrund für den korrigierten KI-Schatten ist ungültig"
        ) from exc

    # Photoroom only creates the transparent vehicle/shadow layer here. The
    # configured Showroom background is composited locally so it can neither
    # disappear nor be rescaled by the external service.
    finished = Image.alpha_composite(background, shadowed_vehicle)
    output = io.BytesIO()
    finished.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


def compose_photoroom_vehicle_with_shadow(
    background_bytes: bytes,
    background_content_type: str,
    vehicle_cutout_png: bytes,
    options: CompositionOptions,
    settings: Settings,
    *,
    photoroom_sandbox: bool,
    usage_context: ExternalApiUsageContext | None = None,
) -> bytes:
    """Compose an automatic result with the same AI shadow used after QA.

    The locally generated contour shadow remains a non-blocking fallback. This
    keeps uploads processable when the additional shadow request is temporarily
    unavailable, while successful requests produce the same shadow pipeline as
    an operator correction.
    """
    if options.shadow_opacity_percent <= 0:
        return compose_showroom(background_bytes, vehicle_cutout_png, options)

    placed_vehicle = compose_showroom(
        background_bytes,
        vehicle_cutout_png,
        options,
        vehicle_layer_only=True,
    )
    try:
        return create_photoroom_shadowed_composition(
            placed_vehicle,
            background_bytes,
            background_content_type,
            settings,
            shadow_opacity_percent=options.shadow_opacity_percent,
            photoroom_sandbox=photoroom_sandbox,
            usage_context=usage_context,
            usage_operation="automatic_vehicle_shadow",
        )
    except ImageProcessingError:
        logger.warning(
            "Automatic Photoroom shadow failed; using the local contour shadow",
            exc_info=True,
        )
        return compose_showroom(background_bytes, vehicle_cutout_png, options)


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
        # The mask returned by OpenAI, or corrected by an operator, is the
        # authoritative selection. Do not add or subtract calibrated regions
        # here: otherwise the final image can differ from the mask shown in the
        # quality editor.
        replacement_alpha = window_alpha

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
    *,
    vehicle_layer_only: bool = False,
) -> bytes:
    try:
        background = Image.open(io.BytesIO(background_bytes)).convert("RGB")
        vehicle = Image.open(io.BytesIO(vehicle_png_bytes)).convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ImageProcessingError("Ein Eingabebild ist ungültig") from exc

    canvas = (
        Image.new("RGBA", (options.width, options.height), (0, 0, 0, 0))
        if vehicle_layer_only
        else ImageOps.fit(
            background,
            (options.width, options.height),
            method=Image.Resampling.LANCZOS,
        ).convert("RGBA")
    )
    alpha_box = vehicle.getchannel("A").point(
        lambda value: 255 if value >= 128 else 0
    ).getbbox()
    if alpha_box is None:
        raise ImageProcessingError("Die Freistellung enthält kein Fahrzeug")

    frame = VehicleFrame(
        contour=VehicleContour(
            width=alpha_box[2] - alpha_box[0],
            height=alpha_box[3] - alpha_box[1],
        ),
        source_width=vehicle.width,
        source_height=vehicle.height,
        left=alpha_box[0],
        top=alpha_box[1],
        right=alpha_box[2],
        bottom=alpha_box[3],
    )
    contour = frame.contour
    perspective = infer_vehicle_perspective(
        options.capture_step_name,
        contour,
        options.orientation_key,
    )
    if options.preserve_source_framing:
        # A 360° exterior series must retain the exact framing recorded by the
        # photographer. Only the complete source canvas is fitted to the output
        # aspect ratio; the vehicle itself is never independently scaled,
        # centered, or moved to a synthetic ground line.
        scene_adjustment = SceneAdjustment()
    else:
        options = perspective_composition_options(options, contour)
        scene_adjustment = calculate_scene_adjustment(options)
        options = replace(
            options,
            contour_target_area_percent=round(
                options.contour_target_area_percent
                * scene_adjustment.scale_multiplier**2
            ),
        )
    if options.manual_source_framing:
        # The quality editor previews a transform of the complete source canvas.
        # Apply the exact same coordinate system here before cropping transparent
        # margins. This keeps scale and offsets pixel-consistent with the browser.
        vehicle = ImageOps.fit(
            vehicle,
            (options.width, options.height),
            method=Image.Resampling.LANCZOS,
        )
        manual_scale = max(50, min(160, options.vehicle_scale_percent)) / 100
        if abs(manual_scale - 1) >= 0.001:
            vehicle = vehicle.resize(
                (
                    max(1, round(vehicle.width * manual_scale)),
                    max(1, round(vehicle.height * manual_scale)),
                ),
                Image.Resampling.LANCZOS,
            )
        x = round((options.width - vehicle.width) / 2)
        y = round((options.height - vehicle.height) / 2)
        x += round(
            options.width
            * max(-35, min(35, options.vehicle_offset_x_percent))
            / 100
        )
        y += round(
            options.height
            * max(-35, min(35, options.vehicle_offset_y_percent))
            / 100
        )
        transformed_box = vehicle.getchannel("A").point(
            lambda value: 255 if value >= 128 else 0
        ).getbbox()
        if transformed_box is None:
            raise ImageProcessingError("Die Freistellung enthält kein Fahrzeug")
        x += transformed_box[0]
        y += transformed_box[1]
        vehicle = vehicle.crop(transformed_box)
        bottom = y + vehicle.height
        scene_adjustment = SceneAdjustment()
    elif options.preserve_source_framing:
        vehicle = ImageOps.fit(
            vehicle,
            (options.width, options.height),
            method=Image.Resampling.LANCZOS,
        )
        fitted_box = vehicle.getchannel("A").point(
            lambda value: 255 if value >= 128 else 0
        ).getbbox()
        if fitted_box is None:
            raise ImageProcessingError("Die Freistellung enthält kein Fahrzeug")
        x, y = fitted_box[0], fitted_box[1]
        vehicle = vehicle.crop(fitted_box)
        bottom = y + vehicle.height
    else:
        framing = calculate_contour_framing(
            contour,
            output_width=options.width,
            output_height=options.height,
            target_area_percent=options.contour_target_area_percent,
            max_width_percent=options.contour_max_width_percent,
            max_height_percent=options.contour_max_height_percent,
        )
        preserve_original_framing = should_preserve_original_framing(
            frame,
            options=options,
            preferred_framing=framing,
        )
        if preserve_original_framing:
            vehicle = ImageOps.fit(
                vehicle,
                (options.width, options.height),
                method=Image.Resampling.LANCZOS,
            )
            fitted_box = vehicle.getchannel("A").point(
                lambda value: 255 if value >= 128 else 0
            ).getbbox()
            if fitted_box is None:
                raise ImageProcessingError("Die Freistellung enthält kein Fahrzeug")
            x, y = fitted_box[0], fitted_box[1]
            vehicle = vehicle.crop(fitted_box)
            bottom = y + vehicle.height
            scene_adjustment = SceneAdjustment()
        else:
            vehicle = vehicle.crop(alpha_box)
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
                (
                    max(1, int(vehicle.width * scale)),
                    max(1, int(vehicle.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
            x = (options.width - vehicle.width) // 2
            bottom = int(
                options.height
                * max(55, min(98, options.vehicle_bottom_percent))
                / 100
            )
            y = bottom - vehicle.height
        manual_scale = max(50, min(160, options.vehicle_scale_percent)) / 100
        if abs(manual_scale - 1) >= 0.001:
            center_x = x + vehicle.width / 2
            vehicle = vehicle.resize(
                (
                    max(1, round(vehicle.width * manual_scale)),
                    max(1, round(vehicle.height * manual_scale)),
                ),
                Image.Resampling.LANCZOS,
            )
            x = round(center_x - vehicle.width / 2)
            y = round(bottom - vehicle.height)
        x += round(
            options.width
            * max(-35, min(35, options.vehicle_offset_x_percent))
            / 100
        )
        y += round(
            options.height
            * max(-35, min(35, options.vehicle_offset_y_percent))
            / 100
        )
        x = max(-vehicle.width + 40, min(options.width - 40, x))
        y = max(-vehicle.height + 40, min(options.height - 40, y))
        bottom = y + vehicle.height

    if options.brightness_percent != 100:
        rgb = ImageEnhance.Brightness(vehicle.convert("RGB")).enhance(
            max(50, min(150, options.brightness_percent)) / 100
        )
        rgb.putalpha(vehicle.getchannel("A"))
        vehicle = rgb

    reflection_opacity = (
        0
        if vehicle_layer_only
        else max(0, min(60, options.reflection_opacity_percent))
    )
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

    shadow_opacity = (
        0 if vehicle_layer_only else max(0, min(80, options.shadow_opacity_percent))
    )
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
                distance_percent=options.shadow_distance_percent,
                angle_degrees=options.shadow_angle_degrees,
                spread_percent=options.shadow_spread_percent,
                blur_percent=options.shadow_blur_percent,
                contact_percent=options.shadow_contact_percent,
            ),
        )

    canvas.alpha_composite(vehicle, (x, y))
    output = io.BytesIO()
    if vehicle_layer_only:
        canvas.save(output, format="PNG", optimize=True)
        return output.getvalue()
    canvas.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


def _vehicle_ground_anchors(alpha: Image.Image) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return robust left/right tyre contact anchors in alpha coordinates.

    Each half of the vehicle is measured independently. This matters for
    diagonal views where the distant wheel is visibly higher than the near
    wheel and a shadow based on the single lowest alpha pixel would float
    below half of the vehicle.
    """
    mask = np.asarray(alpha, dtype=np.uint8) >= 128
    active_y, active_x = np.nonzero(mask)
    width, height = alpha.size
    if not len(active_x):
        return ((round(width * 0.2), height - 1), (round(width * 0.8), height - 1))

    active_left = int(active_x.min())
    active_right = int(active_x.max())
    active_width = max(1, active_right - active_left + 1)
    column_bottoms = np.full(width, -1, dtype=np.int32)
    for x_position in np.unique(active_x):
        column_rows = np.flatnonzero(mask[:, x_position])
        if len(column_rows):
            column_bottoms[x_position] = int(column_rows[-1])

    def anchor(start_ratio: float, end_ratio: float) -> tuple[int, int]:
        band_left = max(active_left, round(active_left + active_width * start_ratio))
        band_right = min(active_right, round(active_left + active_width * end_ratio))
        band_x = np.arange(band_left, band_right + 1)
        valid_x = band_x[column_bottoms[band_x] >= 0]
        if not len(valid_x):
            fallback_x = round((band_left + band_right) / 2)
            return (fallback_x, int(active_y.max()))

        valid_bottoms = column_bottoms[valid_x]
        lower_edge = float(np.percentile(valid_bottoms, 94))
        tolerance = max(2, round(height * 0.012))
        contact_x = valid_x[valid_bottoms >= lower_edge - tolerance]
        if not len(contact_x):
            contact_x = valid_x[valid_bottoms == valid_bottoms.max()]
        center_x = int(round(float(np.median(contact_x))))
        center_y = int(round(float(np.median(column_bottoms[contact_x]))))
        return (center_x, center_y)

    return (anchor(0.08, 0.45), anchor(0.55, 0.92))


def _create_vehicle_shadow(
    alpha: Image.Image,
    canvas_size: tuple[int, int],
    *,
    x: int,
    y: int,
    opacity_percent: int,
    perspective: str,
    depth_multiplier: float = 1.0,
    distance_percent: int = 0,
    angle_degrees: int = 90,
    spread_percent: int = 100,
    blur_percent: int = 100,
    contact_percent: int = 100,
) -> Image.Image:
    """Build a grounded showroom shadow without projecting the body shape.

    Only the tyre anchors are derived from the alpha mask. The visible shadow
    consists of a soft underbody ellipse and two compact contact ellipses.
    This prevents bumpers, wheel arches and a sloping underside from becoming
    rectangular blocks or long wedges in diagonal views.
    """
    vehicle_width, vehicle_height = alpha.size
    distance = vehicle_height * max(0, min(20, distance_percent)) / 100
    angle = math.radians(angle_degrees % 360)
    shadow_offset_x = round(
        max(
            -vehicle_width * 0.05,
            min(vehicle_width * 0.05, math.cos(angle) * distance),
        )
    )
    shadow_offset_y = round(
        max(
            -vehicle_height * 0.03,
            min(vehicle_height * 0.03, math.sin(angle) * distance),
        )
    )
    spread = max(50, min(180, spread_percent)) / 100
    blur = max(20, min(200, blur_percent)) / 100

    alpha_array = np.asarray(alpha, dtype=np.uint8)
    active_rows, active_columns = np.nonzero(alpha_array >= 16)
    if not len(active_columns):
        return Image.new("RGBA", canvas_size, (0, 0, 0, 0))

    active_left = int(active_columns.min())
    active_top = int(active_rows.min())
    active_right = int(active_columns.max())
    active_bottom = int(active_rows.max())
    active_width = max(1, active_right - active_left + 1)
    active_height = max(1, active_bottom - active_top + 1)

    left_anchor, right_anchor = _vehicle_ground_anchors(alpha)
    left_ground_y = float(left_anchor[1])
    right_ground_y = float(right_anchor[1])
    anchor_distance = max(1.0, float(right_anchor[0] - left_anchor[0]))
    maximum_ground_delta = active_height * 0.16
    ground_delta = max(
        -maximum_ground_delta,
        min(maximum_ground_delta, right_ground_y - left_ground_y),
    )
    ground_angle = math.degrees(math.atan2(ground_delta, anchor_distance))
    ground_angle = max(-8.0, min(8.0, ground_angle))

    center_x = x + (left_anchor[0] + right_anchor[0]) / 2 + shadow_offset_x
    center_y = y + (left_ground_y + right_ground_y) / 2 + shadow_offset_y
    perspective_depth = {
        "side": 0.052,
        "straight": 0.060,
        "diagonal": 0.068,
    }.get(perspective, 0.060)
    broad_width = max(12, round(active_width * min(1.04, 0.82 * spread)))
    broad_height = max(
        6,
        round(
            active_height
            * perspective_depth
            * max(0.65, min(1.5, depth_multiplier))
        ),
    )
    broad_mask_array = np.zeros(
        (canvas_size[1], canvas_size[0]),
        dtype=np.uint8,
    )
    cv2.ellipse(
        broad_mask_array,
        (round(center_x), round(center_y)),
        (max(1, broad_width // 2), max(1, broad_height // 2)),
        ground_angle,
        0,
        360,
        255,
        -1,
    )
    broad_blur = max(3, round(active_height * 0.022 * blur))
    broad_mask = Image.fromarray(broad_mask_array, mode="L").filter(
        ImageFilter.GaussianBlur(broad_blur)
    )
    broad_opacity = round(255 * opacity_percent / 100 * 0.42)
    broad_mask = broad_mask.point(
        lambda value: min(255, value * broad_opacity // 255)
    )
    broad_shadow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    broad_shadow.putalpha(broad_mask)

    # Never connect the contact zones with a polygon. That would recreate the
    # block artefact whenever the two visible wheels have different heights.
    contact_height = max(3, round(active_height * 0.022))
    contact_width = max(8, round(active_width * 0.16))
    contact_strength = max(0, min(150, contact_percent)) / 100
    contact_alpha = min(
        215,
        round(255 * opacity_percent / 100 * 1.45 * contact_strength),
    )
    contact_canvas = np.zeros((canvas_size[1], canvas_size[0]), dtype=np.uint8)
    for anchor_x, anchor_y in (left_anchor, right_anchor):
        cv2.ellipse(
            contact_canvas,
            (round(x + anchor_x), round(y + anchor_y)),
            (max(1, contact_width // 2), max(1, contact_height // 2)),
            ground_angle,
            0,
            360,
            255,
            -1,
        )
    contact_mask = Image.fromarray(contact_canvas, mode="L").filter(
        ImageFilter.GaussianBlur(max(2, round(contact_height * 0.65)))
    )
    contact_mask = contact_mask.point(
        lambda value: min(255, value * contact_alpha // 255)
    )
    contact_shadow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    contact_shadow.putalpha(contact_mask)
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
        is_full_canvas = (
            layer.width_percent >= 100
            and overlay.width * canvas.height == overlay.height * canvas.width
        )
        if is_full_canvas:
            if overlay.size != canvas.size:
                overlay = overlay.resize(canvas.size, Image.Resampling.LANCZOS)
        else:
            overlay = overlay.crop(alpha_box)
            target_width = max(
                1, round(canvas.width * max(5, min(100, layer.width_percent)) / 100)
            )
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

        if is_full_canvas:
            canvas.alpha_composite(overlay, (0, 0))
            continue

        horizontal_margin = min(margin, max(0, canvas.width - overlay.width))
        vertical_margin = min(margin, max(0, canvas.height - overlay.height))
        positions = {
            "top_left": (horizontal_margin, vertical_margin),
            "top_right": (
                canvas.width - overlay.width - horizontal_margin,
                vertical_margin,
            ),
            "bottom_left": (
                horizontal_margin,
                canvas.height - overlay.height - vertical_margin,
            ),
            "bottom_right": (
                canvas.width - overlay.width - horizontal_margin,
                canvas.height - overlay.height - vertical_margin,
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
            output_width = min(settings.output_width, 3240)
            output_height = (
                min(round(output_width * 2 / 3), 2160)
                if processing_mode == "exterior_360"
                else settings.output_height
            )
            preview_cutout: bytes | None = None
            composed_background = background_image
            composed_background_content_type = background.content_type
            if processing_mode not in MASKED_BACKGROUND_MODES:
                composed_background = transform_background(
                    background_image,
                    width=output_width,
                    height=output_height,
                    zoom_percent=composition.background_zoom_percent,
                    offset_x_percent=composition.background_offset_x_percent,
                    offset_y_percent=composition.background_offset_y_percent,
                )
                composed_background_content_type = "image/jpeg"
            if processing_mode in MASKED_BACKGROUND_MODES:
                if image_settings.provider != "photoroom":
                    raise ImageProcessingError(
                        "Die maskierte Hintergrundverarbeitung benötigt Photoroom"
                    )
                profile = masked_background_profile(
                    orientation.key if orientation else "",
                    processing_mode,
                    custom_prompt=orientation.mask_prompt if orientation else None,
                    custom_negative_prompt=(
                        orientation.mask_negative_prompt if orientation else None
                    ),
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
                        prompt_template=image_settings.openai_mask_prompt_template,
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
            elif photo.vehicle_mask_is_manual and photo.preview_cutout_object_key:
                manual_mask = storage.get_object(
                    object_key=photo.preview_cutout_object_key
                )
                preview_cutout = apply_cutout_mask_to_original(original, manual_mask)
                shadow_opacity_percent = (
                    photo.vehicle_shadow_opacity_percent
                    if photo.vehicle_shadow_opacity_percent is not None
                    else composition.shadow_opacity_percent
                )
                correction_options = CompositionOptions(
                    width=output_width,
                    height=output_height,
                    contour_target_area_percent=composition.contour_target_area_percent,
                    contour_max_width_percent=composition.contour_max_width_percent,
                    contour_max_height_percent=composition.contour_max_height_percent,
                    vehicle_bottom_percent=composition.vehicle_bottom_percent,
                    shadow_opacity_percent=shadow_opacity_percent,
                    shadow_distance_percent=(
                        photo.vehicle_shadow_distance_percent
                        if photo.vehicle_shadow_distance_percent is not None
                        else 0
                    ),
                    shadow_angle_degrees=(
                        photo.vehicle_shadow_angle_degrees
                        if photo.vehicle_shadow_angle_degrees is not None
                        else 90
                    ),
                    shadow_spread_percent=(
                        photo.vehicle_shadow_spread_percent
                        if photo.vehicle_shadow_spread_percent is not None
                        else 100
                    ),
                    shadow_blur_percent=(
                        photo.vehicle_shadow_blur_percent
                        if photo.vehicle_shadow_blur_percent is not None
                        else 100
                    ),
                    shadow_contact_percent=(
                        photo.vehicle_shadow_contact_percent
                        if photo.vehicle_shadow_contact_percent is not None
                        else 100
                    ),
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
                    vehicle_scale_percent=photo.vehicle_scale_percent,
                    vehicle_offset_x_percent=photo.vehicle_offset_x_percent,
                    vehicle_offset_y_percent=photo.vehicle_offset_y_percent,
                    manual_source_framing=True,
                )
                if shadow_opacity_percent > 0:
                    if image_settings.provider != "photoroom":
                        raise ImageProcessingError(
                            "Der KI-Hauptschatten nach einer Korrektur benötigt Photoroom"
                        )
                    placed_vehicle = compose_showroom(
                        composed_background,
                        preview_cutout,
                        correction_options,
                        vehicle_layer_only=True,
                    )
                    finished = create_photoroom_shadowed_composition(
                        placed_vehicle,
                        composed_background,
                        composed_background_content_type,
                        settings,
                        shadow_opacity_percent=shadow_opacity_percent,
                        photoroom_sandbox=photoroom_sandbox_active(
                            image_settings, settings
                        ),
                        usage_context=usage_context,
                    )
                else:
                    finished = compose_showroom(
                        composed_background,
                        preview_cutout,
                        correction_options,
                    )
                photo.quality_review_required = True
                photo.quality_review_reason = (
                    "Das manuell korrigierte Optimierungsergebnis wartet auf die "
                    "Operator-Freigabe."
                )
                photo.quality_score = 100
                photo.quality_issues = []
                photo.quality_model_version = (
                    "optimized-manual-correction-photoroom-shadow-v2"
                    if shadow_opacity_percent > 0
                    else "optimized-manual-correction-v1"
                )
                if photo.quality_review_created_at is None:
                    photo.quality_review_created_at = datetime.now(timezone.utc)
                photo.quality_reviewed_by_id = None
                photo.quality_reviewed_at = None
                photo.quality_review_resolution = "awaiting_operator_approval"
            elif image_settings.provider == "photoroom":
                photoroom_cutout = create_photoroom_cutout(
                    original,
                    settings,
                    photoroom_sandbox_active(image_settings, settings),
                    usage_context=usage_context,
                )
                preview_cutout = (
                    apply_cutout_mask_to_original(original, photoroom_cutout)
                    if processing_mode == "exterior_360"
                    else photoroom_cutout
                )
                automatic_options = CompositionOptions(
                    width=output_width,
                    height=output_height,
                    contour_target_area_percent=(
                        composition.contour_target_area_percent
                    ),
                    contour_max_width_percent=(
                        composition.contour_max_width_percent
                    ),
                    contour_max_height_percent=(
                        composition.contour_max_height_percent
                    ),
                    vehicle_bottom_percent=composition.vehicle_bottom_percent,
                    shadow_opacity_percent=composition.shadow_opacity_percent,
                    reflection_opacity_percent=(
                        composition.reflection_opacity_percent
                    ),
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
                    preserve_source_framing=processing_mode == "exterior_360",
                )
                # All exterior photos use the same AI-main-shadow path as an
                # operator correction. For the 360° exterior series,
                # preserve_source_framing keeps the original vehicle position,
                # size and aspect ratio while PhotoRoom only adds the shadow.
                finished = (
                    compose_photoroom_vehicle_with_shadow(
                        composed_background,
                        composed_background_content_type,
                        preview_cutout,
                        automatic_options,
                        settings,
                        photoroom_sandbox=photoroom_sandbox_active(
                            image_settings, settings
                        ),
                        usage_context=usage_context,
                    )
                    if processing_mode in {"optimized", "exterior_360"}
                    else compose_showroom(
                        composed_background,
                        preview_cutout,
                        automatic_options,
                    )
                )
            elif image_settings.provider == "remove_bg":
                ai_cutout = remove_vehicle_background(
                    original, settings, usage_context=usage_context
                )
                cutout = apply_cutout_mask_to_original(original, ai_cutout)
                preview_cutout = cutout
                finished = compose_showroom(
                    composed_background,
                    cutout,
                    CompositionOptions(
                        width=output_width,
                        height=output_height,
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
                        preserve_source_framing=processing_mode == "exterior_360",
                    ),
                )
            else:
                raise ImageProcessingError("Die Bildverarbeitung ist deaktiviert")
            if preview_cutout is not None:
                preview_cutout_key = (
                    f"dealerships/{job.dealership_id}/jobs/{job.id}/preview-cutouts/"
                    f"{step.id}/{photo.id}.png"
                )
                storage.put_object(
                    object_key=preview_cutout_key,
                    content=preview_cutout,
                    content_type="image/png",
                )
                photo.preview_cutout_object_key = preview_cutout_key
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
            composed_background = background_image
            composed_background_content_type = background.content_type
            if (
                orientation is None
                or orientation.processing_mode not in MASKED_BACKGROUND_MODES
            ):
                composed_background = transform_background(
                    background_image,
                    width=settings.output_width,
                    height=settings.output_height,
                    zoom_percent=composition.background_zoom_percent,
                    offset_x_percent=composition.background_offset_x_percent,
                    offset_y_percent=composition.background_offset_y_percent,
                )
                composed_background_content_type = "image/jpeg"
            if (
                orientation is not None
                and orientation.processing_mode in MASKED_BACKGROUND_MODES
            ):
                profile = masked_background_profile(
                    orientation.key,
                    orientation.processing_mode,
                    custom_prompt=orientation.mask_prompt,
                    custom_negative_prompt=orientation.mask_negative_prompt,
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
                        prompt_template=image_settings.openai_mask_prompt_template,
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
                    composed_background,
                    composed_background_content_type,
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
