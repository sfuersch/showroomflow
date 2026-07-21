from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.database import SessionLocal
from app.models import ExternalApiUsage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExternalApiUsageContext:
    dealership_id: uuid.UUID
    vehicle_job_id: uuid.UUID
    photo_asset_id: uuid.UUID
    processing_attempt: int | None = None


def record_external_api_usage(
    context: ExternalApiUsageContext | None,
    *,
    provider: str,
    operation: str,
    sandbox: bool,
    outcome: str,
    duration_ms: int,
    http_status: int | None = None,
    error_message: str | None = None,
) -> None:
    """Persist billing-relevant provider usage without breaking image processing."""
    if context is None:
        return
    try:
        with SessionLocal() as db:
            db.add(
                ExternalApiUsage(
                    provider=provider,
                    operation=operation,
                    dealership_id=context.dealership_id,
                    vehicle_job_id=context.vehicle_job_id,
                    photo_asset_id=context.photo_asset_id,
                    processing_attempt=context.processing_attempt,
                    sandbox=sandbox,
                    outcome=outcome,
                    http_status=http_status,
                    duration_ms=max(0, duration_ms),
                    error_message=(error_message or "")[:500] or None,
                )
            )
            db.commit()
    except Exception:
        logger.exception("External image API usage could not be recorded")
