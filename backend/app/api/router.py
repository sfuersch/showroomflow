from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import get_settings
from app.schemas import AppInfoResponse, HealthResponse

router = APIRouter()


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


@router.get("/app-info", response_model=AppInfoResponse, tags=["system"])
def app_info() -> AppInfoResponse:
    return AppInfoResponse(
        name="ShowroomFlow",
        version="0.1.0",
        minimum_ios_version="17.0",
        output_width=1920,
        output_height=1440,
    )
