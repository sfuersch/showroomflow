from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    Dealership,
    SystemImageSettings,
    VehicleCreditUsage,
    VehicleJob,
)

SYSTEM_IMAGE_SETTINGS_ID = 1
IMAGE_PROVIDERS = {"disabled", "remove_bg", "photoroom"}


class VehicleCreditsExhausted(RuntimeError):
    """The dealership has no vehicle credits left in the current month."""


@dataclass(frozen=True)
class VehicleCreditBalance:
    allowance: int
    used: int

    @property
    def available(self) -> int:
        return max(0, self.allowance - self.used)


def month_start(value: date | None = None) -> date:
    current = value or datetime.now(timezone.utc).date()
    return current.replace(day=1)


def get_image_settings(db: Session) -> SystemImageSettings:
    image_settings = db.get(SystemImageSettings, SYSTEM_IMAGE_SETTINGS_ID)
    if image_settings is None:
        runtime = get_settings()
        fallback_provider = (
            runtime.processing_provider
            if runtime.processing_provider in IMAGE_PROVIDERS
            else "disabled"
        )
        image_settings = SystemImageSettings(
            id=SYSTEM_IMAGE_SETTINGS_ID,
            provider=fallback_provider,
            photoroom_sandbox=runtime.photoroom_sandbox,
            default_monthly_vehicle_credits=30,
        )
        db.add(image_settings)
        db.flush()
    return image_settings


def provider_is_available(image_settings: SystemImageSettings, runtime: Settings) -> bool:
    if image_settings.provider == "remove_bg":
        return bool(runtime.remove_bg_api_key)
    if image_settings.provider == "photoroom":
        return bool(runtime.photoroom_api_key)
    return False


def photoroom_sandbox_active(
    image_settings: SystemImageSettings,
    runtime: Settings,
) -> bool:
    return image_settings.photoroom_sandbox or bool(
        runtime.photoroom_api_key and runtime.photoroom_api_key.startswith("sandbox_")
    )


def credit_balance(
    db: Session,
    dealership: Dealership,
    *,
    period: date | None = None,
) -> VehicleCreditBalance:
    current_period = month_start(period)
    used = db.scalar(
        select(func.count(VehicleCreditUsage.id)).where(
            VehicleCreditUsage.dealership_id == dealership.id,
            VehicleCreditUsage.period_start == current_period,
        )
    )
    return VehicleCreditBalance(
        allowance=dealership.monthly_vehicle_credits,
        used=int(used or 0),
    )


def reserve_vehicle_credit(
    db: Session,
    job: VehicleJob,
    provider: str,
    *,
    period: date | None = None,
) -> VehicleCreditUsage:
    dealership = db.scalar(
        select(Dealership).where(Dealership.id == job.dealership_id).with_for_update()
    )
    if dealership is None:
        raise VehicleCreditsExhausted("Autohaus wurde nicht gefunden")
    existing = db.scalar(
        select(VehicleCreditUsage).where(VehicleCreditUsage.vehicle_job_id == job.id)
    )
    if existing is not None:
        return existing
    current_period = month_start(period)
    balance = credit_balance(db, dealership, period=current_period)
    if balance.available <= 0:
        raise VehicleCreditsExhausted("Monatliches Fahrzeug-Credit-Kontingent ist aufgebraucht")

    usage = VehicleCreditUsage(
        id=uuid.uuid4(),
        dealership_id=dealership.id,
        vehicle_job_id=job.id,
        provider=provider,
        period_start=current_period,
        consumed_at=datetime.now(timezone.utc),
    )
    db.add(usage)
    db.flush()
    return usage
