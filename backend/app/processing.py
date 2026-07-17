from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.image_service import (
    get_image_settings,
    photoroom_sandbox_active,
    provider_is_available,
)
from app.models import (
    Background,
    CaptureStep,
    JobStatus,
    PhotoAsset,
    PhotoProcessingVariant,
    ProcessingStatus,
    VehicleJob,
)
from app.storage import ObjectStorage


class ImageProcessingError(RuntimeError):
    """An image could not be processed into a showroom image."""


@dataclass(frozen=True)
class CompositionOptions:
    width: int = 1920
    height: int = 1440
    vehicle_scale_percent: int = 78
    vehicle_bottom_percent: int = 90
    shadow_opacity_percent: int = 32
    reflection_opacity_percent: int = 10
    brightness_percent: int = 100


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


def create_photoroom_showroom(
    original_bytes: bytes,
    background_bytes: bytes,
    background_content_type: str,
    settings: Settings,
    vehicle_scale_percent: int = 60,
    vehicle_bottom_percent: int = 90,
    photoroom_sandbox: bool | None = None,
    optimized: bool = False,
    *,
    client: httpx.Client | None = None,
) -> bytes:
    """Create a standard or color-preserving optimized Photoroom result."""
    request = client.post if client is not None else httpx.post
    background_extension = "png" if background_content_type == "image/png" else "jpg"
    edit_options = {
        "removeBackground": "true",
        "background.color": "FFFFFF",
        "shadow.mode": "ai.soft",
        "outputSize": f"{settings.output_width}x{settings.output_height}",
        "paddingLeft": f"{max(0.02, (1 - vehicle_scale_percent / 100) / 2):.3f}",
        "paddingRight": f"{max(0.02, (1 - vehicle_scale_percent / 100) / 2):.3f}",
        "paddingBottom": f"{max(0.02, 1 - vehicle_bottom_percent / 100):.3f}",
        "horizontalAlignment": "center",
        "verticalAlignment": "bottom",
        "export.format": "jpeg",
    }
    if optimized:
        edit_options.update(
            {
                "lighting.mode": "ai.preserve-hue-and-saturation",
                "ignorePaddingAndSnapOnCroppedSides": "false",
            }
        )
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
    alpha_box = vehicle.getchannel("A").getbbox()
    if alpha_box is None:
        raise ImageProcessingError("Die Freistellung enthält kein Fahrzeug")
    vehicle = vehicle.crop(alpha_box)

    max_width = int(options.width * max(20, min(95, options.vehicle_scale_percent)) / 100)
    max_height = int(options.height * 0.72)
    scale = min(max_width / vehicle.width, max_height / vehicle.height)
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
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(shadow)
        shadow_width = int(vehicle.width * 0.82)
        shadow_height = max(20, int(vehicle.height * 0.10))
        shadow_x = x + (vehicle.width - shadow_width) // 2
        draw.ellipse(
            (
                shadow_x,
                bottom - shadow_height // 2,
                shadow_x + shadow_width,
                bottom + shadow_height // 2,
            ),
            fill=(0, 0, 0, int(255 * shadow_opacity / 100)),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(12, shadow_height // 2)))
        canvas = Image.alpha_composite(canvas, shadow)

    canvas.alpha_composite(vehicle, (x, y))
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


def process_photo(photo_id: str) -> None:
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
            if image_settings.provider == "photoroom":
                finished = create_photoroom_showroom(
                    original,
                    background_image,
                    background.content_type,
                    settings,
                    vehicle_scale_percent=background.vehicle_scale_percent,
                    vehicle_bottom_percent=background.vehicle_bottom_percent,
                    photoroom_sandbox=photoroom_sandbox_active(image_settings, settings),
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
                        vehicle_scale_percent=background.vehicle_scale_percent,
                        vehicle_bottom_percent=background.vehicle_bottom_percent,
                        shadow_opacity_percent=background.shadow_opacity_percent,
                        reflection_opacity_percent=background.reflection_opacity_percent,
                        brightness_percent=background.brightness_percent,
                    ),
                )
            else:
                raise ImageProcessingError("Die Bildverarbeitung ist deaktiviert")
            processed_key = (
                f"dealerships/{job.dealership_id}/jobs/{job.id}/processed/{step.id}/{photo.id}.jpg"
            )
            storage.put_object(
                object_key=processed_key,
                content=finished,
                content_type="image/jpeg",
            )
            photo.processed_object_key = processed_key
            photo.processed_content_type = "image/jpeg"
            photo.processed_size_bytes = len(finished)
            photo.processed_provider = image_settings.provider
            photo.processing_status = ProcessingStatus.COMPLETED
            photo.processing_completed_at = datetime.now(timezone.utc)
            job.status = _next_job_status(db, job.id)
            db.commit()
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
            finished = create_photoroom_showroom(
                original,
                background_image,
                background.content_type,
                settings,
                vehicle_scale_percent=background.vehicle_scale_percent,
                vehicle_bottom_percent=background.vehicle_bottom_percent,
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
            variant.object_key = object_key
            variant.content_type = "image/jpeg"
            variant.size_bytes = len(finished)
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
