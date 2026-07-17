from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import JobStatus, UserRole


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    retention_days: int
    timestamp: datetime


class AppInfoResponse(BaseModel):
    name: str
    version: str
    minimum_ios_version: str
    output_width: int
    output_height: int


class StorageHealthResponse(BaseModel):
    status: str
    provider: str
    bucket: str


class DealershipCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    auto_export_enabled: bool = False
    retention_days: int = Field(default=90, ge=1, le=365)


class DealershipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    auto_export_enabled: bool
    retention_days: int
    is_active: bool
    created_at: datetime


class LocationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    dealership_id: uuid.UUID | None = None


class LocationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dealership_id: uuid.UUID
    name: str
    is_active: bool
    created_at: datetime


class BrandConfigurationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class BackgroundConfigurationResponse(BaseModel):
    id: uuid.UUID
    name: str
    brand_id: uuid.UUID | None
    location_ids: list[uuid.UUID]
    image_url: str


class CaptureStepConfigurationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    instruction: str
    category: str
    capture_order: int
    export_order: int | None
    is_required: bool
    requires_processing: bool
    silhouette_url: str | None = None


class AppConfigurationResponse(BaseModel):
    brands: list[BrandConfigurationResponse]
    backgrounds: list[BackgroundConfigurationResponse]
    capture_steps: list[CaptureStepConfigurationResponse]


class PhotoAssetResponse(BaseModel):
    id: uuid.UUID
    capture_step_id: uuid.UUID
    revision: int
    image_url: str
    uploaded_at: datetime


class CaptureSessionResponse(BaseModel):
    job: "VehicleJobResponse"
    capture_steps: list[CaptureStepConfigurationResponse]
    photos: list[PhotoAssetResponse]


class PhotoUploadRequest(BaseModel):
    capture_step_id: uuid.UUID
    content_type: str = Field(pattern="^image/jpeg$")
    size_bytes: int = Field(ge=1, le=25 * 1024 * 1024)


class PhotoUploadResponse(BaseModel):
    photo_id: uuid.UUID
    revision: int
    upload_url: str
    expires_in: int


class VehicleJobCreateRequest(BaseModel):
    dealership_id: uuid.UUID | None = None
    location_id: uuid.UUID
    vin: str = Field(min_length=1, max_length=64)
    brand: str = Field(min_length=1, max_length=100)
    brand_id: uuid.UUID | None = None
    background_id: uuid.UUID | None = None
    auto_export: bool | None = None


class VehicleJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dealership_id: uuid.UUID
    location_id: uuid.UUID
    created_by_id: uuid.UUID
    vin: str
    version: int
    brand: str
    brand_id: uuid.UUID | None
    background_id: uuid.UUID | None
    status: JobStatus
    auto_export: bool
    created_at: datetime


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dealership_id: uuid.UUID | None
    email: EmailStr
    role: UserRole
    is_active: bool
    created_at: datetime


class UserCreateRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    role: UserRole
    dealership_id: uuid.UUID | None = None


class UserUpdateRequest(BaseModel):
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)
