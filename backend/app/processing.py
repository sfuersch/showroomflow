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
from app.models import (
    Background,
    CaptureStep,
    JobStatus,
    PhotoAsset,
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
    if settings.processing_provider != "remove_bg" or not settings.remove_bg_api_key:
        raise ImageProcessingError("Kein KI-Dienst für die Freistellung konfiguriert")
    try:
        response = httpx.post(
            "https://api.remove.bg/v1.0/removebg",
            headers={"X-Api-Key": settings.remove_bg_api_key},
            files={"image_file": ("vehicle.jpg", image, "image/jpeg")},
            data={"size": "auto", "type": "car", "format": "png"},
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

            photo.processing_status = ProcessingStatus.PROCESSING
            photo.processing_attempts += 1
            photo.processing_error = None
            photo.processing_started_at = datetime.now(timezone.utc)
            job.status = JobStatus.PROCESSING
            db.commit()

            original = storage.get_object(object_key=photo.original_object_key)
            cutout = remove_vehicle_background(original, settings)
            background_image = storage.get_object(object_key=background.object_key)
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
