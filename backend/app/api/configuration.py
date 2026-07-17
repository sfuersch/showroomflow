import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.api.dependencies import CurrentUser, DatabaseSession
from app.models import Background, Brand, CaptureStep, Location, UserRole
from app.schemas import (
    AppConfigurationResponse,
    BackgroundConfigurationResponse,
    BrandConfigurationResponse,
    CaptureStepConfigurationResponse,
)
from app.storage import ObjectStorage, get_object_storage

router = APIRouter(prefix="/configuration", tags=["app configuration"])
StorageDependency = Annotated[ObjectStorage, Depends(get_object_storage)]


@router.get("", response_model=AppConfigurationResponse)
def app_configuration(
    db: DatabaseSession,
    current_user: CurrentUser,
    storage: StorageDependency,
    location_id: uuid.UUID,
    dealership_id: uuid.UUID | None = Query(default=None),
) -> AppConfigurationResponse:
    target_dealership_id = (
        dealership_id if current_user.role == UserRole.SYSTEM_ADMIN else current_user.dealership_id
    )
    if target_dealership_id is None:
        raise HTTPException(status_code=422, detail="Autohaus ist erforderlich")
    location = db.get(Location, location_id)
    if location is None or not location.is_active or location.dealership_id != target_dealership_id:
        raise HTTPException(status_code=422, detail="Standort wurde nicht gefunden")

    brands = list(
        db.scalars(
            select(Brand)
            .where(
                Brand.dealership_id == target_dealership_id,
                Brand.is_active.is_(True),
            )
            .order_by(Brand.name)
        )
    )
    backgrounds = list(
        db.scalars(
            select(Background)
            .options(selectinload(Background.locations))
            .where(
                Background.dealership_id == target_dealership_id,
                Background.is_active.is_(True),
                or_(
                    ~Background.locations.any(),
                    Background.locations.any(Location.id == location.id),
                ),
            )
            .order_by(Background.name)
        )
    )
    steps = list(
        db.scalars(
            select(CaptureStep)
            .where(
                CaptureStep.dealership_id == target_dealership_id,
                CaptureStep.is_active.is_(True),
            )
            .order_by(CaptureStep.capture_order, CaptureStep.name)
        )
    )

    return AppConfigurationResponse(
        brands=[BrandConfigurationResponse.model_validate(brand) for brand in brands],
        backgrounds=[
            BackgroundConfigurationResponse(
                id=background.id,
                name=background.name,
                brand_id=background.brand_id,
                location_ids=[item.id for item in background.locations],
                image_url=storage.create_download_url(object_key=background.object_key),
            )
            for background in backgrounds
        ],
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
    )
