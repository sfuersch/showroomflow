import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["retention_days"] == 90


def test_app_info() -> None:
    response = client.get("/api/v1/app-info")
    assert response.status_code == 200
    assert response.json()["name"] == "ShowroomFlow"
    assert response.json()["output_width"] == 1920
    assert response.json()["output_height"] == 1440


def test_processing_uses_preview_size_by_default() -> None:
    assert Settings().remove_bg_size == "preview"


def test_photoroom_comparison_uses_sandbox_by_default() -> None:
    settings = Settings(photoroom_api_key="test-key")
    assert settings.photoroom_enabled is True
    assert settings.photoroom_sandbox is True


def test_photoroom_selects_separate_live_and_sandbox_keys() -> None:
    settings = Settings(
        photoroom_live_api_key="live-key",
        photoroom_sandbox_api_key="sandbox-key",
    )

    assert settings.photoroom_enabled is True
    assert settings.photoroom_key_for(sandbox=True) == "sandbox-key"
    assert settings.photoroom_key_for(sandbox=False) == "live-key"


def test_legacy_photoroom_key_remains_compatible() -> None:
    settings = Settings(photoroom_api_key="legacy-key")

    assert settings.photoroom_key_for(sandbox=True) == "sandbox_legacy-key"
    assert settings.photoroom_key_for(sandbox=False) == "legacy-key"


def test_production_rejects_placeholder_secrets() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", secret_key="replace-with-an-insecure-placeholder-value")


def test_production_accepts_complete_secure_configuration() -> None:
    settings = Settings(
        environment="production",
        secret_key="a" * 64,
        database_url="postgresql+psycopg://user:secret@db:5432/showroomflow",
        redis_url="redis://:secret@redis:6379/0",
        storage_endpoint="https://s3.example.com",
        storage_region="eu-central-1",
        storage_access_key="production-access-key",
        storage_secret_key="production-secret-key",
        storage_bucket="showroomflow-production",
    )
    assert settings.environment == "production"
