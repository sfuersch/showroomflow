import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, DatabaseSession
from app.models import Dealership, JobStatus, Location, User, UserRole, VehicleJob
from app.schemas import VehicleJobCreateRequest, VehicleJobResponse

router = APIRouter(prefix="/jobs", tags=["vehicle jobs"])


def _target_dealership(user: User, requested_id: uuid.UUID | None) -> uuid.UUID | None:
    if user.role != UserRole.SYSTEM_ADMIN:
        return user.dealership_id
    return requested_id


@router.get("", response_model=list[VehicleJobResponse])
def list_jobs(
    db: DatabaseSession,
    current_user: CurrentUser,
    dealership_id: uuid.UUID | None = Query(default=None),
) -> list[VehicleJob]:
    statement = select(VehicleJob).order_by(VehicleJob.created_at.desc())
    target_id = _target_dealership(current_user, dealership_id)
    if current_user.role != UserRole.SYSTEM_ADMIN or target_id is not None:
        statement = statement.where(VehicleJob.dealership_id == target_id)
    return list(db.scalars(statement))


@router.post("", response_model=VehicleJobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: VehicleJobCreateRequest,
    db: DatabaseSession,
    current_user: CurrentUser,
) -> VehicleJob:
    location = db.get(Location, payload.location_id)
    requested_dealership_id = payload.dealership_id
    if current_user.role == UserRole.SYSTEM_ADMIN and requested_dealership_id is None:
        requested_dealership_id = location.dealership_id if location is not None else None
    dealership_id = _target_dealership(current_user, requested_dealership_id)
    if dealership_id is None:
        raise HTTPException(status_code=422, detail="Autohaus ist erforderlich")

    dealership = db.get(Dealership, dealership_id, with_for_update=True)
    if dealership is None:
        raise HTTPException(status_code=422, detail="Autohaus wurde nicht gefunden")
    if location is None or location.dealership_id != dealership_id:
        raise HTTPException(status_code=422, detail="Standort wurde nicht gefunden")

    vin = payload.vin.strip().upper()
    brand = payload.brand.strip()
    if not vin or not brand:
        raise HTTPException(status_code=422, detail="VIN und Marke sind erforderlich")

    latest_version = db.scalar(
        select(func.max(VehicleJob.version)).where(
            VehicleJob.dealership_id == dealership_id,
            VehicleJob.vin == vin,
        )
    )
    job = VehicleJob(
        dealership_id=dealership_id,
        location_id=location.id,
        created_by_id=current_user.id,
        vin=vin,
        version=(latest_version or 0) + 1,
        brand=brand,
        status=JobStatus.DRAFT,
        auto_export=(
            dealership.auto_export_enabled
            if payload.auto_export is None
            else payload.auto_export
        ),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Auftrag konnte wegen einer parallelen Anlage nicht erstellt werden",
        ) from None
    db.refresh(job)
    return job
