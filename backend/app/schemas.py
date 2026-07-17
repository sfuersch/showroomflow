from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import UserRole


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
