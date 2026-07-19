from collections.abc import Generator
import io

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
        self.put_call: dict[str, object] | None = None
        self.head_object_call: dict[str, object] | None = None
        self.get_object_call: dict[str, object] | None = None
        self.delete_object_call: dict[str, object] | None = None

    def head_bucket(self, *, Bucket: str) -> None:
        self.head_bucket_name = Bucket

    def generate_presigned_url(self, operation: str, **kwargs: object) -> str:
        self.presign_call = {"operation": operation, **kwargs}
        return "https://signed.example/upload"

    def put_object(self, **kwargs: object) -> None:
        self.put_call = kwargs

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.head_object_call = kwargs
        return {"ContentLength": 1234, "ContentType": "image/jpeg"}

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.get_object_call = kwargs
        return {"Body": io.BytesIO(b"stored-image")}

    def delete_object(self, **kwargs: object) -> None:
        self.delete_object_call = kwargs


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


def test_private_configuration_image_can_be_uploaded_and_read_with_signed_url() -> None:
    fake_client = FakeS3Client()
    storage = ObjectStorage(Settings(storage_bucket="test-bucket"), client=fake_client)

    storage.put_object(
        object_key="dealerships/one/configuration/backgrounds/image.jpg",
        content=b"image",
        content_type="image/jpeg",
    )
    url = storage.create_download_url(
        object_key="dealerships/one/configuration/backgrounds/image.jpg",
        expires_in=300,
    )

    assert fake_client.put_call == {
        "Bucket": "test-bucket",
        "Key": "dealerships/one/configuration/backgrounds/image.jpg",
        "Body": b"image",
        "ContentType": "image/jpeg",
    }
    assert url == "https://signed.example/upload"
    assert fake_client.presign_call == {
        "operation": "get_object",
        "Params": {
            "Bucket": "test-bucket",
            "Key": "dealerships/one/configuration/backgrounds/image.jpg",
        },
        "ExpiresIn": 300,
        "HttpMethod": "GET",
    }


def test_download_url_can_force_a_safe_attachment_filename() -> None:
    fake_client = FakeS3Client()
    storage = ObjectStorage(Settings(storage_bucket="test-bucket"), client=fake_client)

    storage.create_download_url(
        object_key="dealerships/one/jobs/job/photo.jpg",
        filename='VIN 01 "Original".jpg',
    )

    assert fake_client.presign_call == {
        "operation": "get_object",
        "Params": {
            "Bucket": "test-bucket",
            "Key": "dealerships/one/jobs/job/photo.jpg",
            "ResponseContentDisposition": 'attachment; filename="VIN_01__Original_.jpg"',
        },
        "ExpiresIn": 900,
        "HttpMethod": "GET",
    }


def test_uploaded_photo_metadata_is_verified() -> None:
    fake_client = FakeS3Client()
    storage = ObjectStorage(Settings(storage_bucket="test-bucket"), client=fake_client)

    metadata = storage.object_metadata(object_key="dealerships/one/jobs/job/photo.jpg")

    assert metadata == (1234, "image/jpeg")
    assert fake_client.head_object_call == {
        "Bucket": "test-bucket",
        "Key": "dealerships/one/jobs/job/photo.jpg",
    }


def test_private_object_can_be_downloaded_by_worker() -> None:
    fake_client = FakeS3Client()
    storage = ObjectStorage(Settings(storage_bucket="test-bucket"), client=fake_client)

    content = storage.get_object(object_key="dealerships/one/jobs/job/photo.jpg")

    assert content == b"stored-image"
    assert fake_client.get_object_call == {
        "Bucket": "test-bucket",
        "Key": "dealerships/one/jobs/job/photo.jpg",
    }


def test_private_object_can_be_deleted() -> None:
    fake_client = FakeS3Client()
    storage = ObjectStorage(Settings(storage_bucket="test-bucket"), client=fake_client)

    storage.delete_object(object_key="dealerships/one/configuration/background.jpg")

    assert fake_client.delete_object_call == {
        "Bucket": "test-bucket",
        "Key": "dealerships/one/configuration/background.jpg",
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
