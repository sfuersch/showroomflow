from functools import lru_cache

from pydantic import Field
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
    secret_key: str = Field(default="development-only-change-me", min_length=24)
    database_url: str = "postgresql+psycopg://showroomflow:showroomflow@db:5432/showroomflow"
    redis_url: str = "redis://redis:6379/0"
    storage_endpoint: str = "http://minio:9000"
    storage_access_key: str = "showroomflow"
    storage_secret_key: str = "development-only-change-me"
    storage_bucket: str = "showroomflow"
    retention_days: int = Field(default=90, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
