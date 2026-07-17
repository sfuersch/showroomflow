from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserRole(str, enum.Enum):
    SYSTEM_ADMIN = "system_admin"
    DEALERSHIP_ADMIN = "dealership_admin"
    PHOTOGRAPHER = "photographer"


class JobStatus(str, enum.Enum):
    DRAFT = "draft"
    CAPTURING = "capturing"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    REVIEW_REQUIRED = "review_required"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Dealership(Timestamped, Base):
    __tablename__ = "dealerships"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160))
    auto_export_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=90)

    locations: Mapped[list["Location"]] = relationship(back_populates="dealership")


class Location(Timestamped, Base):
    __tablename__ = "locations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dealerships.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))

    dealership: Mapped[Dealership] = relationship(back_populates="locations")


class User(Timestamped, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dealerships.id"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class VehicleJob(Timestamped, Base):
    __tablename__ = "vehicle_jobs"
    __table_args__ = (
        UniqueConstraint("dealership_id", "vin", "version", name="uq_vehicle_job_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dealerships.id"), index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("locations.id"), index=True)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    vin: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    brand: Mapped[str] = mapped_column(String(100))
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.DRAFT)
    auto_export: Mapped[bool] = mapped_column(Boolean, default=False)


class ExportRun(Timestamped, Base):
    __tablename__ = "export_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    vehicle_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicle_jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    zip_filename: Mapped[str] = mapped_column(String(255))
    successful: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
