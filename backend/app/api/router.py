from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from app.api.dependencies import DatabaseSession
from app.config import get_settings
from app.schemas import AppInfoResponse, HealthResponse
from app.api.auth import router as auth_router
from app.api.users import router as users_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(users_router)


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service="showroomflow-api",
        environment=settings.environment,
        retention_days=settings.retention_days,
        timestamp=datetime.now(timezone.utc),
    )


@router.get("/ready", response_model=HealthResponse, tags=["system"])
def ready(db: DatabaseSession) -> HealthResponse:
    db.execute(text("SELECT 1"))
    return health()


@router.get("/app-info", response_model=AppInfoResponse, tags=["system"])
def app_info() -> AppInfoResponse:
    return AppInfoResponse(
        name="ShowroomFlow",
        version="0.1.0",
        minimum_ios_version="17.0",
        output_width=1920,
        output_height=1440,
    )
