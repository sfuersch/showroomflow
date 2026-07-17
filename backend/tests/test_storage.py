from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_current_user
from app.config import Settings
from app.main import app
from app.models import User, UserRole
from app.storage import ObjectStorage, StorageUnavailableError, get_object_storage

client = TestClient(app)


class FakeS3Client:
    def __init__(self) -> None:
        self.head_bucket_name: str | None = None
        self.presign_call: dict[str, object] | None = None

    def head_bucket(self, *, Bucket: str) -> None:
        self.head_bucket_name = Bucket

    def generate_presigned_url(self, operation: str, **kwargs: object) -> str:
        self.presign_call = {"operation": operation, **kwargs}
        return "https://signed.example/upload"


class UnavailableStorage:
    bucket = "showroomflow-production"

    def check_connection(self) -> None:
        raise StorageUnavailableError("unavailable")


class AvailableStorage:
    bucket = "showroomflow-production"

    def check_connection(self) -> None:
        return None


def system_admin() -> User:
    return User(
        dealership_id=None,
        email="system@example.com",
        password_hash="not-used",
        role=UserRole.SYSTEM_ADMIN,
    )


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> Generator[None, None, None]:
    existing = app.dependency_overrides.copy()
    yield
    app.dependency_overrides = existing


def test_storage_connection_uses_configured_bucket() -> None:
    fake_client = FakeS3Client()
    storage = ObjectStorage(Settings(storage_bucket="test-bucket"), client=fake_client)

    storage.check_connection()

    assert fake_client.head_bucket_name == "test-bucket"


def test_presigned_upload_is_limited_to_content_type_and_expiry() -> None:
    fake_client = FakeS3Client()
    storage = ObjectStorage(Settings(storage_bucket="test-bucket"), client=fake_client)

    url = storage.create_upload_url(
        object_key="dealership/job/photo/original.jpg",
        content_type="image/jpeg",
        expires_in=600,
    )

    assert url == "https://signed.example/upload"
    assert fake_client.presign_call == {
        "operation": "put_object",
        "Params": {
            "Bucket": "test-bucket",
            "Key": "dealership/job/photo/original.jpg",
            "ContentType": "image/jpeg",
        },
        "ExpiresIn": 600,
        "HttpMethod": "PUT",
    }


def test_storage_health_requires_system_admin() -> None:
    app.dependency_overrides[get_current_user] = lambda: User(
        dealership_id=None,
        email="photo@example.com",
        password_hash="not-used",
        role=UserRole.PHOTOGRAPHER,
    )

    response = client.get("/api/v1/admin/storage/health")

    assert response.status_code == 403


def test_storage_health_hides_provider_error() -> None:
    app.dependency_overrides[get_current_user] = system_admin
    app.dependency_overrides[get_object_storage] = UnavailableStorage

    response = client.get("/api/v1/admin/storage/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Bildspeicher ist nicht erreichbar"}


def test_storage_health_reports_configured_bucket() -> None:
    app.dependency_overrides[get_current_user] = system_admin
    app.dependency_overrides[get_object_storage] = AvailableStorage

    response = client.get("/api/v1/admin/storage/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "provider": "cloudflare-r2",
        "bucket": "showroomflow-production",
    }
