from functools import lru_cache

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
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    database_url: str = "postgresql+psycopg://showroomflow:showroomflow@db:5432/showroomflow"
    redis_url: str = "redis://redis:6379/0"
    storage_endpoint: str = "http://minio:9000"
    storage_region: str = "us-east-1"
    storage_access_key: str = "showroomflow"
    storage_secret_key: str = "development-only-change-me"
    storage_bucket: str = "showroomflow"
    retention_days: int = Field(default=90, ge=1)

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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
