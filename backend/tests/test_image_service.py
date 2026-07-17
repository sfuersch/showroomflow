import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.image_service import (
    VehicleCreditsExhausted,
    credit_balance,
    reserve_vehicle_credit,
)
from app.models import Dealership, Location, User, UserRole, VehicleJob


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
