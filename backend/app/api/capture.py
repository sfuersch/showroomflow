import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update

from app.api.dependencies import CurrentUser, DatabaseSession
from app.config import get_settings
from app.image_service import (
    VehicleCreditsExhausted,
    get_image_settings,
    provider_is_available,
    reserve_vehicle_credit,
)
from app.models import (
    CaptureStep,
    JobStatus,
    PhotoAsset,
    ProcessingStatus,
    UserRole,
    VehicleJob,
)
from app.processing_queue import ProcessingQueueUnavailable, enqueue_photo_processing
from app.schemas import (
    CaptureSessionResponse,
    CaptureStepConfigurationResponse,
    PhotoAssetResponse,
    PhotoUploadRequest,
    PhotoUploadResponse,
    VehicleJobResponse,
)
from app.storage import (
    ObjectStorage,
    StorageObjectNotFoundError,
    StorageUnavailableError,
    get_object_storage,
)

router = APIRouter(prefix="/jobs/{job_id}/capture", tags=["photo capture"])
StorageDependency = Annotated[ObjectStorage, Depends(get_object_storage)]
UPLOAD_URL_SECONDS = 900


def _authorized_job(db: DatabaseSession, user: CurrentUser, job_id: uuid.UUID) -> VehicleJob:
    job = db.get(VehicleJob, job_id)
    if job is None or (
        user.role != UserRole.SYSTEM_ADMIN and job.dealership_id != user.dealership_id
    ):
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    return job


def _photo_response(storage: ObjectStorage, photo: PhotoAsset) -> PhotoAssetResponse:
    if photo.uploaded_at is None:
        raise ValueError("Only uploaded photos can be returned")
    return PhotoAssetResponse(
        id=photo.id,
        capture_step_id=photo.capture_step_id,
        revision=photo.revision,
        image_url=storage.create_download_url(object_key=photo.original_object_key),
        processed_image_url=(
            storage.create_download_url(object_key=photo.processed_object_key)
            if photo.processed_object_key
            else None
        ),
        processing_status=photo.processing_status,
        processing_error=photo.processing_error,
        uploaded_at=photo.uploaded_at,
    )


@router.get("", response_model=CaptureSessionResponse)
def capture_session(
    job_id: uuid.UUID,
    db: DatabaseSession,
    current_user: CurrentUser,
    storage: StorageDependency,
) -> CaptureSessionResponse:
    job = _authorized_job(db, current_user, job_id)
    steps = list(
        db.scalars(
            select(CaptureStep)
            .where(
                CaptureStep.dealership_id == job.dealership_id,
                CaptureStep.is_active.is_(True),
            )
            .order_by(CaptureStep.capture_order, CaptureStep.name)
        )
    )
    photos = list(
        db.scalars(
            select(PhotoAsset)
            .where(
                PhotoAsset.vehicle_job_id == job.id,
                PhotoAsset.is_selected.is_(True),
                PhotoAsset.uploaded_at.is_not(None),
            )
            .order_by(PhotoAsset.created_at)
        )
    )
    return CaptureSessionResponse(
        job=VehicleJobResponse.model_validate(job),
        capture_steps=[
            CaptureStepConfigurationResponse(
                id=step.id,
                name=step.name,
                instruction=step.instruction,
                category=step.category,
                capture_order=step.capture_order,
                export_order=step.export_order,
                is_required=step.is_required,
                requires_processing=step.requires_processing,
                silhouette_url=(
                    storage.create_download_url(object_key=step.silhouette_object_key)
                    if step.silhouette_object_key
                    else None
                ),
            )
            for step in steps
        ],
        photos=[_photo_response(storage, photo) for photo in photos],
    )


@router.post("/uploads", response_model=PhotoUploadResponse, status_code=status.HTTP_201_CREATED)
def create_photo_upload(
    job_id: uuid.UUID,
    payload: PhotoUploadRequest,
    db: DatabaseSession,
    current_user: CurrentUser,
    storage: StorageDependency,
) -> PhotoUploadResponse:
    job = _authorized_job(db, current_user, job_id)
    if job.status in {JobStatus.EXPORTING, JobStatus.COMPLETED}:
        raise HTTPException(status_code=409, detail="Der Auftrag kann nicht mehr bearbeitet werden")
    step = db.get(CaptureStep, payload.capture_step_id)
    if step is None or not step.is_active or step.dealership_id != job.dealership_id:
        raise HTTPException(status_code=422, detail="Fotoposition wurde nicht gefunden")

    locked_job = db.get(VehicleJob, job.id, with_for_update=True)
    if locked_job is None:
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    latest_revision = db.scalar(
        select(func.max(PhotoAsset.revision)).where(
            PhotoAsset.vehicle_job_id == job.id,
            PhotoAsset.capture_step_id == step.id,
        )
    )
    revision = (latest_revision or 0) + 1
    photo_id = uuid.uuid4()
    object_key = f"dealerships/{job.dealership_id}/jobs/{job.id}/originals/{step.id}/{photo_id}.jpg"
    photo = PhotoAsset(
        id=photo_id,
        vehicle_job_id=job.id,
        capture_step_id=step.id,
        captured_by_id=current_user.id,
        revision=revision,
        original_object_key=object_key,
        original_content_type=payload.content_type,
        expected_size_bytes=payload.size_bytes,
    )
    db.add(photo)
    locked_job.status = JobStatus.CAPTURING
    db.commit()
    return PhotoUploadResponse(
        photo_id=photo.id,
        revision=photo.revision,
        upload_url=storage.create_upload_url(
            object_key=photo.original_object_key,
            content_type=photo.original_content_type,
            expires_in=UPLOAD_URL_SECONDS,
        ),
        expires_in=UPLOAD_URL_SECONDS,
    )


@router.post("/photos/{photo_id}/complete", response_model=PhotoAssetResponse)
def complete_photo_upload(
    job_id: uuid.UUID,
    photo_id: uuid.UUID,
    db: DatabaseSession,
    current_user: CurrentUser,
    storage: StorageDependency,
) -> PhotoAssetResponse:
    job = _authorized_job(db, current_user, job_id)
    photo = db.get(PhotoAsset, photo_id)
    if photo is None or photo.vehicle_job_id != job.id:
        raise HTTPException(status_code=404, detail="Foto wurde nicht gefunden")
    try:
        size_bytes, content_type = storage.object_metadata(object_key=photo.original_object_key)
    except StorageObjectNotFoundError as exc:
        raise HTTPException(status_code=409, detail="Foto wurde noch nicht hochgeladen") from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Bildspeicher ist nicht erreichbar") from exc
    if content_type != photo.original_content_type or size_bytes != photo.expected_size_bytes:
        raise HTTPException(status_code=409, detail="Das hochgeladene Foto ist unvollständig")

    db.execute(
        update(PhotoAsset)
        .where(
            PhotoAsset.vehicle_job_id == job.id,
            PhotoAsset.capture_step_id == photo.capture_step_id,
            PhotoAsset.id != photo.id,
        )
        .values(is_selected=False)
    )
    photo.original_size_bytes = size_bytes
    photo.uploaded_at = datetime.now(timezone.utc)
    photo.is_selected = True
    step = db.get(CaptureStep, photo.capture_step_id)
    runtime = get_settings()
    image_settings = get_image_settings(db)
    should_enqueue = bool(
        step and step.requires_processing and provider_is_available(image_settings, runtime)
    )
    credit_error: str | None = None
    if should_enqueue:
        try:
            reserve_vehicle_credit(db, job, image_settings.provider)
        except VehicleCreditsExhausted as exc:
            should_enqueue = False
            credit_error = str(exc)
    if step and step.requires_processing:
        photo.processing_status = (
            ProcessingStatus.QUEUED if should_enqueue else ProcessingStatus.PENDING
        )
        job.status = JobStatus.PROCESSING if should_enqueue else JobStatus.REVIEW_REQUIRED
        photo.processing_error = credit_error
    else:
        photo.processing_status = ProcessingStatus.NOT_REQUIRED
    db.commit()
    db.refresh(photo)
    if should_enqueue:
        try:
            enqueue_photo_processing(photo.id)
        except ProcessingQueueUnavailable:
            photo.processing_status = ProcessingStatus.FAILED
            photo.processing_error = "Verarbeitungswarteschlange ist nicht erreichbar"
            job.status = JobStatus.REVIEW_REQUIRED
            db.commit()
            db.refresh(photo)
    return _photo_response(storage, photo)
