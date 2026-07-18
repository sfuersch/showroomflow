from __future__ import annotations

import io
import re
import uuid
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import (
    CaptureStep,
    DealershipSftpSettings,
    ExportRun,
    JobStatus,
    PhotoAsset,
    ProcessingStatus,
    SupplementalImage,
    VehicleJob,
)
from app.storage import ObjectStorage


class ExportValidationError(RuntimeError):
    """The effective export configuration cannot produce a safe archive."""


@dataclass(frozen=True)
class ExportItem:
    order: int
    name: str
    object_key: str


def safe_vin(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value.strip())
    return cleaned or "FAHRZEUG"


def _supplemental_matches(item: SupplementalImage, job: VehicleJob) -> bool:
    if item.brand_id is not None and item.brand_id != job.brand_id:
        return False
    return not item.locations or any(location.id == job.location_id for location in item.locations)


def validate_export_items(items: list[ExportItem]) -> list[ExportItem]:
    if not items:
        raise ExportValidationError(
            "Für diesen Auftrag sind keine exportierbaren Bilder vorhanden."
        )
    occupied: dict[int, str] = {}
    for item in items:
        existing = occupied.get(item.order)
        if existing is not None:
            raise ExportValidationError(
                f"Exportplatz {item.order} ist doppelt belegt: {existing} und {item.name}."
            )
        occupied[item.order] = item.name
    return sorted(items, key=lambda item: (item.order, item.name))


def resolve_export_items(db: Session, job: VehicleJob) -> list[ExportItem]:
    rows = list(
        db.execute(
            select(PhotoAsset, CaptureStep)
            .join(CaptureStep, CaptureStep.id == PhotoAsset.capture_step_id)
            .where(
                PhotoAsset.vehicle_job_id == job.id,
                PhotoAsset.is_selected.is_(True),
                PhotoAsset.uploaded_at.is_not(None),
                CaptureStep.is_active.is_(True),
                CaptureStep.export_order.is_not(None),
            )
        ).all()
    )
    supplemental_images = list(
        db.scalars(
            select(SupplementalImage)
            .options(selectinload(SupplementalImage.locations))
            .where(
                SupplementalImage.dealership_id == job.dealership_id,
                SupplementalImage.is_active.is_(True),
            )
            .order_by(SupplementalImage.export_order, SupplementalImage.name)
        )
    )

    items: list[ExportItem] = []
    for photo, step in rows:
        if step.requires_processing:
            if (
                photo.processing_status != ProcessingStatus.COMPLETED
                or not photo.processed_object_key
            ):
                raise ExportValidationError(
                    f'Fotoposition "{step.name}" ist noch nicht vollständig verarbeitet.'
                )
            object_key = photo.processed_object_key
        else:
            object_key = photo.original_object_key
        items.append(ExportItem(int(step.export_order), step.name, object_key))

    items.extend(
        ExportItem(item.export_order, item.name, item.object_key)
        for item in supplemental_images
        if _supplemental_matches(item, job)
    )
    return validate_export_items(items)


def normalize_export_jpeg(content: bytes, settings: Settings) -> bytes:
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
    except (OSError, ValueError) as exc:
        raise ExportValidationError("Ein Exportbild ist beschädigt oder ungültig.") from exc
    image = ImageOps.fit(
        image,
        (settings.output_width, settings.output_height),
        method=Image.Resampling.LANCZOS,
    )
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


def build_zip_bytes(
    vin: str,
    items: list[ExportItem],
    storage: ObjectStorage,
    settings: Settings,
) -> bytes:
    archive = io.BytesIO()
    filename_prefix = safe_vin(vin)
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_STORED) as zip_file:
        for item in validate_export_items(items):
            content = storage.get_object(object_key=item.object_key)
            zip_file.writestr(
                f"{filename_prefix}_{item.order:02d}.jpg",
                normalize_export_jpeg(content, settings),
            )
    return archive.getvalue()


def try_enqueue_auto_export(job_id: uuid.UUID, session: Session | None = None) -> None:
    """Queue one automatic export after capture and selected photos are ready."""
    session_context = SessionLocal() if session is None else nullcontext(session)
    with session_context as db:
        job = db.scalar(select(VehicleJob).where(VehicleJob.id == job_id).with_for_update())
        if job is None or not job.auto_export or job.capture_completed_at is None:
            return
        existing = db.scalar(select(ExportRun.id).where(ExportRun.vehicle_job_id == job.id))
        if existing is not None:
            return
        required_steps = list(
            db.scalars(
                select(CaptureStep).where(
                    CaptureStep.dealership_id == job.dealership_id,
                    CaptureStep.is_active.is_(True),
                    CaptureStep.is_required.is_(True),
                )
            )
        )
        photos = {
            photo.capture_step_id: photo
            for photo in db.scalars(
                select(PhotoAsset).where(
                    PhotoAsset.vehicle_job_id == job.id,
                    PhotoAsset.is_selected.is_(True),
                    PhotoAsset.uploaded_at.is_not(None),
                )
            )
        }
        if any(step.id not in photos for step in required_steps):
            return
        active_steps = {
            step.id: step
            for step in db.scalars(
                select(CaptureStep).where(
                    CaptureStep.dealership_id == job.dealership_id,
                    CaptureStep.is_active.is_(True),
                )
            )
        }
        if any(
            active_steps.get(photo.capture_step_id)
            and active_steps[photo.capture_step_id].requires_processing
            and photo.processing_status != ProcessingStatus.COMPLETED
            for photo in photos.values()
        ):
            return

        export_run = ExportRun(
            vehicle_job_id=job.id,
            attempt=1,
            zip_filename=f"{safe_vin(job.vin)}.zip",
            status="queued",
        )
        db.add(export_run)
        try:
            resolve_export_items(db, job)
        except ExportValidationError as exc:
            export_run.status = "failed"
            export_run.error_message = str(exc)[:1000]
            job.status = JobStatus.REVIEW_REQUIRED
            db.commit()
            return
        db.commit()
        db.refresh(export_run)

    from app.processing_queue import (  # Avoid a module import cycle.
        ProcessingQueueUnavailable,
        enqueue_vehicle_export,
    )

    try:
        enqueue_vehicle_export(export_run.id)
    except ProcessingQueueUnavailable as exc:
        with SessionLocal() as db:
            failed_run = db.get(ExportRun, export_run.id)
            if failed_run is not None:
                failed_run.status = "failed"
                failed_run.error_message = str(exc)[:1000]
                failed_job = db.get(VehicleJob, failed_run.vehicle_job_id)
                if failed_job is not None:
                    failed_job.status = JobStatus.REVIEW_REQUIRED
                db.commit()


def process_export_run(export_run_id: str) -> None:
    identifier = uuid.UUID(export_run_id)
    settings = get_settings()
    storage = ObjectStorage(settings)
    queue_transfer = False
    try:
        with SessionLocal() as db:
            export_run = db.get(ExportRun, identifier)
            if export_run is None:
                return
            job = db.get(VehicleJob, export_run.vehicle_job_id)
            if job is None:
                raise ExportValidationError("Fahrzeugauftrag wurde nicht gefunden.")
            export_run.status = "processing"
            export_run.error_message = None
            job.status = JobStatus.EXPORTING
            db.commit()

            items = resolve_export_items(db, job)
            archive = build_zip_bytes(job.vin, items, storage, settings)
            object_key = (
                f"dealerships/{job.dealership_id}/jobs/{job.id}/exports/"
                f"{export_run.id}/{safe_vin(job.vin)}.zip"
            )
            storage.put_object(
                object_key=object_key,
                content=archive,
                content_type="application/zip",
            )
            export_run.object_key = object_key
            export_run.size_bytes = len(archive)
            export_run.status = "completed"
            export_run.successful = True
            export_run.completed_at = datetime.now(timezone.utc)
            job.status = JobStatus.COMPLETED
            sftp_config = db.get(DealershipSftpSettings, job.dealership_id)
            queue_transfer = bool(
                job.auto_export and sftp_config is not None and sftp_config.is_enabled
            )
            if queue_transfer:
                export_run.transfer_status = "queued"
            db.commit()
        if queue_transfer:
            from app.processing_queue import (  # Avoid a module import cycle.
                ProcessingQueueUnavailable,
                enqueue_export_transfer,
            )

            try:
                enqueue_export_transfer(identifier)
            except ProcessingQueueUnavailable as exc:
                with SessionLocal() as db:
                    export_run = db.get(ExportRun, identifier)
                    if export_run is not None:
                        export_run.transfer_status = "failed"
                        export_run.transfer_error = str(exc)[:1000]
                        job = db.get(VehicleJob, export_run.vehicle_job_id)
                        if job is not None:
                            job.status = JobStatus.REVIEW_REQUIRED
                        db.commit()
    except Exception as exc:
        with SessionLocal() as db:
            export_run = db.get(ExportRun, identifier)
            if export_run is not None:
                export_run.status = "failed"
                export_run.successful = False
                export_run.error_message = str(exc)[:1000]
                job = db.get(VehicleJob, export_run.vehicle_job_id)
                if job is not None:
                    job.status = JobStatus.REVIEW_REQUIRED
                db.commit()
        raise
