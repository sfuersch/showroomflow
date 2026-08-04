import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.image_service import (
    VehicleCreditsExhausted,
    credit_balance,
    grant_additional_credits,
    orientation_processing_provider,
    provider_is_available,
    reserve_vehicle_credit,
)
from app.models import (
    Dealership,
    Location,
    Orientation,
    User,
    UserRole,
    VehicleCreditUsage,
    VehicleJob,
)
from app.orientations import orientation_requires_processing


@pytest.mark.parametrize(
    ("processing_mode", "processing_provider", "expected"),
    [
        ("optimized", "photoroom", True),
        ("optimized", "remove_bg", True),
        ("optimized", "openai", True),
        ("optimized", "original", False),
        ("original", "photoroom", False),
    ],
)
def test_orientation_requires_processing_respects_provider(
    processing_mode: str,
    processing_provider: str,
    expected: bool,
) -> None:
    assert (
        orientation_requires_processing(processing_mode, processing_provider)
        is expected
    )


def test_orientation_provider_overrides_global_provider() -> None:
    image_settings = SimpleNamespace(provider="photoroom")
    orientation = Orientation(
        key="front",
        name="Front",
        category="exterior",
        default_capture_order=1,
        processing_provider="remove_bg",
    )

    assert orientation_processing_provider(orientation, image_settings) == "remove_bg"
    assert orientation_processing_provider(None, image_settings) == "photoroom"


def test_provider_availability_uses_explicit_orientation_provider() -> None:
    image_settings = SimpleNamespace(provider="photoroom")
    runtime = SimpleNamespace(
        openai_api_key="openai-key",
        remove_bg_api_key="remove-bg-key",
    )

    assert provider_is_available(image_settings, runtime, "openai") is True
    assert provider_is_available(image_settings, runtime, "remove_bg") is True
    assert provider_is_available(image_settings, runtime, "original") is True


def create_job(db: Session, dealership: Dealership, user: User, location: Location) -> VehicleJob:
    job = VehicleJob(
        dealership_id=dealership.id,
        location_id=location.id,
        created_by_id=user.id,
        vin=f"TEST-{uuid.uuid4()}",
        version=1,
        brand="Test",
    )
    db.add(job)
    db.flush()
    return job


def test_one_credit_covers_complete_vehicle_and_refills_monthly() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        dealership = Dealership(name="Test Autohaus", monthly_vehicle_credits=1)
        db.add(dealership)
        db.flush()
        location = Location(dealership_id=dealership.id, name="Standort")
        user = User(
            dealership_id=dealership.id,
            email="photo@example.com",
            password_hash="test",
            role=UserRole.PHOTOGRAPHER,
        )
        db.add_all([location, user])
        db.flush()
        first_job = create_job(db, dealership, user, location)
        second_job = create_job(db, dealership, user, location)

        first_usage = reserve_vehicle_credit(
            db,
            first_job,
            "photoroom",
            period=date(2026, 7, 18),
        )
        repeated_usage = reserve_vehicle_credit(
            db,
            first_job,
            "photoroom",
            period=date(2026, 7, 18),
        )

        assert repeated_usage.id == first_usage.id
        assert credit_balance(db, dealership, period=date(2026, 7, 31)).available == 0
        with pytest.raises(VehicleCreditsExhausted):
            reserve_vehicle_credit(
                db,
                second_job,
                "photoroom",
                period=date(2026, 7, 31),
            )

        reserve_vehicle_credit(
            db,
            second_job,
            "photoroom",
            period=date(2026, 8, 1),
        )
        august = credit_balance(db, dealership, period=date(2026, 8, 20))
        assert august.allowance == 1
        assert august.used == 1
        assert august.available == 0


def test_additional_credits_carry_over_and_are_used_after_monthly_credits() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        dealership = Dealership(
            name="Test Autohaus",
            monthly_vehicle_credits=0,
            additional_vehicle_credits=0,
        )
        db.add(dealership)
        db.flush()
        location = Location(dealership_id=dealership.id, name="Standort")
        admin = User(
            dealership_id=None,
            email="system@example.com",
            password_hash="test",
            role=UserRole.SYSTEM_ADMIN,
        )
        photographer = User(
            dealership_id=dealership.id,
            email="photo@example.com",
            password_hash="test",
            role=UserRole.PHOTOGRAPHER,
        )
        db.add_all([location, admin, photographer])
        db.flush()
        july_job = create_job(db, dealership, photographer, location)
        august_job = create_job(db, dealership, photographer, location)

        grant = grant_additional_credits(
            db,
            dealership.id,
            admin,
            2,
            "Einmaliges Aktionskontingent",
        )
        july_usage = reserve_vehicle_credit(
            db,
            july_job,
            "photoroom",
            period=date(2026, 7, 31),
        )
        august_usage = reserve_vehicle_credit(
            db,
            august_job,
            "photoroom",
            period=date(2026, 8, 1),
        )

        assert grant.amount == 2
        assert grant.note == "Einmaliges Aktionskontingent"
        assert july_usage.credit_source == "additional"
        assert august_usage.credit_source == "additional"
        assert db.get(Dealership, dealership.id).additional_vehicle_credits == 0
        assert credit_balance(db, dealership, period=date(2026, 9, 1)).available == 0
        assert len(db.query(VehicleCreditUsage).all()) == 2
