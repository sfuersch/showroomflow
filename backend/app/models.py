from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Dealership(Timestamped, Base):
    __tablename__ = "dealerships"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160))
    auto_export_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=90)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    locations: Mapped[list["Location"]] = relationship(back_populates="dealership")


class Location(Timestamped, Base):
    __tablename__ = "locations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dealerships.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    dealership: Mapped[Dealership] = relationship(back_populates="locations")


class Brand(Timestamped, Base):
    __tablename__ = "brands"
    __table_args__ = (UniqueConstraint("dealership_id", "name", name="uq_brand_dealership_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dealerships.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class BackgroundLocation(Base):
    __tablename__ = "background_locations"

    background_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("backgrounds.id", ondelete="CASCADE"), primary_key=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), primary_key=True
    )


class Background(Timestamped, Base):
    __tablename__ = "backgrounds"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dealerships.id"), index=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brands.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    locations: Mapped[list[Location]] = relationship(secondary="background_locations")


class CaptureStep(Timestamped, Base):
    __tablename__ = "capture_steps"
    __table_args__ = (
        UniqueConstraint("dealership_id", "name", name="uq_capture_step_dealership_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dealerships.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    instruction: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(32), default="detail")
    capture_order: Mapped[int] = mapped_column(Integer)
    export_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    requires_processing: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    silhouette_object_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    silhouette_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)


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


class RefreshSession(Timestamped, Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brands.id"), nullable=True, index=True
    )
    background_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("backgrounds.id"), nullable=True, index=True
    )
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.DRAFT)
    auto_export: Mapped[bool] = mapped_column(Boolean, default=False)


class PhotoAsset(Timestamped, Base):
    __tablename__ = "photo_assets"
    __table_args__ = (
        UniqueConstraint(
            "vehicle_job_id",
            "capture_step_id",
            "revision",
            name="uq_photo_asset_revision",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    vehicle_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicle_jobs.id"), index=True)
    capture_step_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_steps.id"), index=True)
    captured_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    original_object_key: Mapped[str] = mapped_column(String(500), unique=True)
    original_content_type: Mapped[str] = mapped_column(String(100))
    expected_size_bytes: Mapped[int] = mapped_column(Integer)
    original_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)


class ExportRun(Timestamped, Base):
    __tablename__ = "export_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    vehicle_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicle_jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    zip_filename: Mapped[str] = mapped_column(String(255))
    successful: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
