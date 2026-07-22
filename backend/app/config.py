from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from SHOWROOMFLOW_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="SHOWROOMFLOW_",
        env_file=".env",
        extra="ignore",
    )

    environment: str = "development"
    public_base_url: str = "https://showroomflow.promotekk.com"
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    secret_key: str = Field(default="development-only-change-me-please", min_length=32)
    access_token_minutes: int = Field(default=30, ge=5, le=1440)
    refresh_token_days: int = Field(default=30, ge=1, le=90)
    admin_session_hours: int = Field(default=8, ge=1, le=24)
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    database_url: str = "postgresql+psycopg://showroomflow:showroomflow@db:5432/showroomflow"
    redis_url: str = "redis://redis:6379/0"
    storage_endpoint: str = "http://minio:9000"
    storage_region: str = "auto"
    storage_access_key: str = "showroomflow"
    storage_secret_key: str = "development-only-change-me"
    storage_bucket: str = "showroomflow"
    retention_days: int = Field(default=90, ge=1)
    processing_provider: Literal["disabled", "remove_bg"] = "disabled"
    processing_queue: str = "showroomflow-processing"
    remove_bg_api_key: str | None = None
    remove_bg_size: Literal["preview", "auto", "full", "50MP"] = "preview"
    # `photoroom_api_key` remains as a backwards-compatible transition path.
    photoroom_api_key: str | None = None
    photoroom_live_api_key: str | None = None
    photoroom_sandbox_api_key: str | None = None
    photoroom_sandbox: bool = True
    output_width: int = Field(default=1920, ge=640, le=7680)
    output_height: int = Field(default=1440, ge=480, le=4320)
    web_push_vapid_public_key: str | None = None
    web_push_vapid_private_key: str | None = None
    web_push_vapid_subject: str | None = None

    @property
    def processing_enabled(self) -> bool:
        return self.processing_provider != "disabled"

    @property
    def web_push_enabled(self) -> bool:
        return bool(self.web_push_vapid_public_key and self.web_push_vapid_private_key)

    @property
    def web_push_subject(self) -> str:
        return self.web_push_vapid_subject or self.public_base_url

    @property
    def photoroom_enabled(self) -> bool:
        return bool(
            self.photoroom_live_api_key
            or self.photoroom_sandbox_api_key
            or self.photoroom_api_key
        )

    def photoroom_key_for(self, *, sandbox: bool) -> str | None:
        """Select the explicit key for the requested Photoroom environment."""
        if sandbox:
            if self.photoroom_sandbox_api_key:
                return self.photoroom_sandbox_api_key
            if not self.photoroom_api_key:
                return None
            if self.photoroom_api_key.startswith("sandbox_"):
                return self.photoroom_api_key
            return f"sandbox_{self.photoroom_api_key}"
        if self.photoroom_live_api_key:
            return self.photoroom_live_api_key
        if self.photoroom_api_key and not self.photoroom_api_key.startswith("sandbox_"):
            return self.photoroom_api_key
        return None

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.environment != "production":
            return self
        if self.secret_key.startswith("development") or "replace" in self.secret_key.lower():
            raise ValueError("SHOWROOMFLOW_SECRET_KEY must be replaced for production")
        if not self.public_base_url.startswith("https://"):
            raise ValueError("SHOWROOMFLOW_PUBLIC_BASE_URL must use HTTPS in production")
        protected_values = {
            "SHOWROOMFLOW_DATABASE_URL": self.database_url,
            "SHOWROOMFLOW_REDIS_URL": self.redis_url,
            "SHOWROOMFLOW_STORAGE_ENDPOINT": self.storage_endpoint,
            "SHOWROOMFLOW_STORAGE_ACCESS_KEY": self.storage_access_key,
            "SHOWROOMFLOW_STORAGE_SECRET_KEY": self.storage_secret_key,
            "SHOWROOMFLOW_STORAGE_BUCKET": self.storage_bucket,
        }
        for name, value in protected_values.items():
            if not value or "replace" in value.lower():
                raise ValueError(f"{name} must be configured for production")
        if not self.storage_endpoint.startswith("https://"):
            raise ValueError("SHOWROOMFLOW_STORAGE_ENDPOINT must use HTTPS in production")
        if self.bootstrap_admin_password and (
            len(self.bootstrap_admin_password) < 16
            or "replace" in self.bootstrap_admin_password.lower()
        ):
            raise ValueError("Bootstrap administrator password is not secure")
        if self.processing_provider == "remove_bg" and not self.remove_bg_api_key:
            raise ValueError("SHOWROOMFLOW_REMOVE_BG_API_KEY is required for remove_bg")
        if bool(self.web_push_vapid_public_key) != bool(self.web_push_vapid_private_key):
            raise ValueError("Both SHOWROOMFLOW_WEB_PUSH_VAPID keys must be configured")
        if self.web_push_vapid_subject and not self.web_push_vapid_subject.startswith(
            ("https://", "mailto:")
        ):
            raise ValueError("SHOWROOMFLOW_WEB_PUSH_VAPID_SUBJECT must be HTTPS or mailto")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
