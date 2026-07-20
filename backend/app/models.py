from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
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


class ProcessingStatus(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
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
    monthly_vehicle_credits: Mapped[int] = mapped_column(Integer, default=30)
    additional_vehicle_credits: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    locations: Mapped[list["Location"]] = relationship(back_populates="dealership")
    sftp_settings: Mapped["DealershipSftpSettings | None"] = relationship(
        back_populates="dealership", cascade="all, delete-orphan", uselist=False
    )


class DealershipSftpSettings(Timestamped, Base):
    __tablename__ = "dealership_sftp_settings"

    dealership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dealerships.id", ondelete="CASCADE"), primary_key=True
    )
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(255), default="")
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_directory: Mapped[str] = mapped_column(String(500), default="/")
    host_key_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_test_successful: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    dealership: Mapped[Dealership] = relationship(back_populates="sftp_settings")


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
    vehicle_scale_percent: Mapped[int] = mapped_column(Integer, default=78)
    vehicle_bottom_percent: Mapped[int] = mapped_column(Integer, default=90)
    shadow_opacity_percent: Mapped[int] = mapped_column(Integer, default=32)
    reflection_opacity_percent: Mapped[int] = mapped_column(Integer, default=10)
    brightness_percent: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    locations: Mapped[list[Location]] = relationship(secondary="background_locations")


class ImageOverlayLocation(Base):
    __tablename__ = "image_overlay_locations"

    overlay_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("image_overlays.id", ondelete="CASCADE"), primary_key=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), primary_key=True
    )


class ImageOverlayCaptureStep(Base):
    __tablename__ = "image_overlay_capture_steps"

    overlay_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("image_overlays.id", ondelete="CASCADE"), primary_key=True
    )
    capture_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("capture_steps.id", ondelete="CASCADE"), primary_key=True
    )


class ImageOverlay(Timestamped, Base):
    __tablename__ = "image_overlays"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dealerships.id", ondelete="CASCADE"), index=True
    )
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brands.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    content_type: Mapped[str] = mapped_column(String(100), default="image/png")
    position: Mapped[str] = mapped_column(String(32), default="bottom_right")
    width_percent: Mapped[int] = mapped_column(Integer, default=18)
    opacity_percent: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    locations: Mapped[list[Location]] = relationship(secondary="image_overlay_locations")
    capture_steps: Mapped[list["CaptureStep"]] = relationship(
        secondary="image_overlay_capture_steps"
    )


class SupplementalImageLocation(Base):
    __tablename__ = "supplemental_image_locations"

    supplemental_image_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplemental_images.id", ondelete="CASCADE"), primary_key=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), primary_key=True
    )


class SupplementalImage(Timestamped, Base):
    __tablename__ = "supplemental_images"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dealerships.id", ondelete="CASCADE"), index=True
    )
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brands.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    export_order: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    locations: Mapped[list[Location]] = relationship(secondary="supplemental_image_locations")


class Orientation(Timestamped, Base):
    __tablename__ = "orientations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    instruction: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(32), default="detail")
    default_capture_order: Mapped[int] = mapped_column(Integer)
    default_export_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    requires_processing: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CaptureStep(Timestamped, Base):
    __tablename__ = "capture_steps"
    __table_args__ = (
        UniqueConstraint("dealership_id", "name", name="uq_capture_step_dealership_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dealerships.id"), index=True)
    orientation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orientations.id", ondelete="SET NULL"), nullable=True, index=True
    )
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

    orientation: Mapped[Orientation | None] = relationship()


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
    capture_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
    original_thumbnail_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True, unique=True
    )
    benchmark_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True, unique=True
    )
    benchmark_thumbnail_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True, unique=True
    )
    benchmark_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    benchmark_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus), default=ProcessingStatus.NOT_REQUIRED
    )
    processed_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True, unique=True
    )
    processed_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    processed_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_thumbnail_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True, unique=True
    )
    processed_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0)
    processing_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PhotoProcessingVariant(Timestamped, Base):
    __tablename__ = "photo_processing_variants"
    __table_args__ = (
        UniqueConstraint(
            "photo_asset_id",
            "provider",
            name="uq_photo_processing_variant_provider",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    photo_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("photo_assets.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default=ProcessingStatus.PENDING.value)
    object_key: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True, unique=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SystemImageSettings(Timestamped, Base):
    __tablename__ = "system_image_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(32), default="remove_bg")
    photoroom_sandbox: Mapped[bool] = mapped_column(Boolean, default=True)
    comparison_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_monthly_vehicle_credits: Mapped[int] = mapped_column(Integer, default=30)
    contour_target_area_percent: Mapped[int] = mapped_column(Integer, default=36)
    contour_max_width_percent: Mapped[int] = mapped_column(Integer, default=78)
    contour_max_height_percent: Mapped[int] = mapped_column(Integer, default=72)


class VehicleCreditUsage(Base):
    __tablename__ = "vehicle_credit_usages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dealerships.id", ondelete="CASCADE"), index=True
    )
    vehicle_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vehicle_jobs.id", ondelete="CASCADE"), unique=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    credit_source: Mapped[str] = mapped_column(String(32), default="monthly")
    period_start: Mapped[date] = mapped_column(Date, index=True)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class VehicleCreditGrant(Base):
    __tablename__ = "vehicle_credit_grants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dealership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dealerships.id", ondelete="CASCADE"), index=True
    )
    granted_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(500), default="")
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ExportRun(Timestamped, Base):
    __tablename__ = "export_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    vehicle_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicle_jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    zip_filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    object_key: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    successful: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    transfer_status: Mapped[str] = mapped_column(String(32), default="not_requested")
    transfer_attempts: Mapped[int] = mapped_column(Integer, default=0)
    transferred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    remote_path: Mapped[str | None] = mapped_column(String(700), nullable=True)
    transfer_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
