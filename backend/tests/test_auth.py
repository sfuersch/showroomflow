import io
import uuid
from collections.abc import Generator
from datetime import datetime, timezone
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.exporting import try_enqueue_auto_export
from app.models import (
    Background,
    BackgroundOrientationComposition,
    Brand,
    CaptureStep,
    Dealership,
    ExportRun,
    ImageOverlay,
    JobStatus,
    Location,
    Orientation,
    PhotoAsset,
    ProcessingStatus,
    SupplementalImage,
    SystemImageSettings,
    User,
    UserRole,
    VehicleCreditGrant,
    VehicleJob,
)
from app.security import hash_password
from app.storage import get_object_storage

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def override_db() -> Generator[Session, None, None]:
    with TestingSession() as session:
        yield session


app.dependency_overrides[get_db] = override_db
client = TestClient(app)


class ConfigurationStorage:
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []
        self.deleted_keys: list[str] = []

    def put_object(self, **values: object) -> None:
        self.uploads.append(values)

    def get_object(self, *, object_key: str) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (1920, 1440), "navy").save(output, format="JPEG")
        return output.getvalue()

    def create_download_url(
        self,
        *,
        object_key: str,
        expires_in: int = 900,
        filename: str | None = None,
    ) -> str:
        return f"https://images.example/{object_key}?expires={expires_in}"

    def create_upload_url(
        self, *, object_key: str, content_type: str, expires_in: int = 900
    ) -> str:
        return f"https://uploads.example/{object_key}?type={content_type}&expires={expires_in}"

    def object_metadata(self, *, object_key: str) -> tuple[int, str]:
        return 1234, "image/jpeg"

    def delete_object(self, *, object_key: str) -> None:
        self.deleted_keys.append(object_key)


@pytest.fixture(autouse=True)
def reset_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def create_dealership_admin() -> tuple[Dealership, User]:
    with TestingSession() as db:
        dealership = Dealership(name="Test Autohaus")
        db.add(dealership)
        db.flush()
        admin = User(
            dealership_id=dealership.id,
            email="admin@example.com",
            password_hash=hash_password("a-secure-test-password"),
            role=UserRole.DEALERSHIP_ADMIN,
        )
        db.add(admin)
        db.commit()
        db.refresh(dealership)
        db.refresh(admin)
        return dealership, admin


def login() -> dict[str, str | int]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "a-secure-test-password"},
    )
    assert response.status_code == 200
    return response.json()


def create_system_admin() -> User:
    with TestingSession() as db:
        admin = User(
            dealership_id=None,
            email="system@example.com",
            password_hash=hash_password("a-secure-system-password"),
            role=UserRole.SYSTEM_ADMIN,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        return admin


def create_operator() -> User:
    with TestingSession() as db:
        operator = User(
            dealership_id=None,
            email="operator@example.com",
            password_hash=hash_password("a-secure-operator-password"),
            role=UserRole.OPERATOR,
        )
        db.add(operator)
        db.commit()
        db.refresh(operator)
        return operator


def system_login() -> dict[str, str | int]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "system@example.com", "password": "a-secure-system-password"},
    )
    assert response.status_code == 200
    return response.json()


def csrf_from(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def jpeg_bytes(color: str = "navy") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1920, 1440), color).save(output, format="JPEG")
    return output.getvalue()


def test_system_admin_configures_image_service_and_dealership_credits() -> None:
    create_system_admin()
    with TestingSession() as db:
        dealership = Dealership(name="Credit Autohaus", monthly_vehicle_credits=30)
        db.add(dealership)
        db.commit()
        db.refresh(dealership)
        dealership_id = dealership.id

    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "system@example.com",
            "password": "a-secure-system-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    settings_page = client.get("/admin/image-service")
    assert settings_page.status_code == 200
    assert "Bilddienstleister und Credits" in settings_page.text
    assert "direkt am jeweiligen Hintergrund" in settings_page.text
    assert 'name="contour_target_area_percent"' not in settings_page.text

    settings_response = client.post(
        "/admin/image-service",
        data={
            "provider": "photoroom",
            "default_monthly_vehicle_credits": "40",
            "photoroom_sandbox": "on",
            "comparison_mode_enabled": "on",
            "csrf_token": csrf_from(settings_page.text),
        },
        follow_redirects=True,
    )
    credits_response = client.post(
        f"/admin/dealerships/{dealership_id}/credits",
        data={
            "monthly_vehicle_credits": "25",
            "csrf_token": csrf_from(settings_response.text),
        },
        follow_redirects=True,
    )
    topup_response = client.post(
        f"/admin/dealerships/{dealership_id}/credits/add",
        data={
            "amount": "7",
            "note": "Kulanz",
            "csrf_token": csrf_from(credits_response.text),
        },
        follow_redirects=True,
    )

    assert topup_response.status_code == 200
    assert "7 zusätzliche Fahrzeug-Credits" in topup_response.text
    with TestingSession() as db:
        image_settings = db.get(SystemImageSettings, 1)
        dealership = db.get(Dealership, dealership_id)
        assert image_settings is not None
        assert image_settings.provider == "photoroom"
        assert image_settings.photoroom_sandbox is True
        assert image_settings.comparison_mode_enabled is True
        assert image_settings.default_monthly_vehicle_credits == 40
        assert dealership is not None
        assert dealership.monthly_vehicle_credits == 25
        assert dealership.additional_vehicle_credits == 7
        grant = db.scalar(select(VehicleCreditGrant))
        assert grant is not None
        assert grant.amount == 7
        assert grant.note == "Kulanz"


def test_dealership_admin_cannot_open_system_image_service_settings() -> None:
    create_dealership_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )

    response = client.get("/admin/image-service")

    assert response.status_code == 403


def test_system_admin_can_disable_comparison_mode() -> None:
    create_system_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "system@example.com",
            "password": "a-secure-system-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    settings_page = client.get("/admin/image-service")

    response = client.post(
        "/admin/image-service",
        data={
            "provider": "disabled",
            "default_monthly_vehicle_credits": "30",
            "contour_target_area_percent": "36",
            "contour_max_width_percent": "78",
            "contour_max_height_percent": "72",
            "csrf_token": csrf_from(settings_page.text),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with TestingSession() as db:
        image_settings = db.get(SystemImageSettings, 1)
        assert image_settings is not None
        assert image_settings.comparison_mode_enabled is False


def test_login_and_current_user() -> None:
    _, admin = create_dealership_admin()
    tokens = login()

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(admin.id)
    assert response.json()["role"] == "dealership_admin"


def test_ready_checks_database() -> None:
    response = client.get("/api/v1/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_refresh_token_is_rotated() -> None:
    create_dealership_admin()
    tokens = login()

    refreshed = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    reused = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
    assert reused.status_code == 401


def test_logout_revokes_refresh_token() -> None:
    create_dealership_admin()
    tokens = login()

    logout_response = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
    )
    refresh_response = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert logout_response.status_code == 204
    assert refresh_response.status_code == 401


def test_dealership_admin_creates_only_users_in_own_dealership() -> None:
    dealership, _ = create_dealership_admin()
    tokens = login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = client.post(
        "/api/v1/admin/users",
        headers=headers,
        json={
            "email": "photo@example.com",
            "password": "another-secure-password",
            "role": "photographer",
            "dealership_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 201
    assert response.json()["dealership_id"] == str(dealership.id)


def test_dealership_admin_cannot_create_system_admin() -> None:
    create_dealership_admin()
    tokens = login()

    response = client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={
            "email": "system@example.com",
            "password": "another-secure-password",
            "role": "system_admin",
        },
    )

    assert response.status_code == 403


def test_system_admin_creates_dealership_and_location() -> None:
    create_system_admin()
    tokens = system_login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    dealership_response = client.post(
        "/api/v1/admin/dealerships",
        headers=headers,
        json={
            "name": "Autohaus Nord",
            "auto_export_enabled": True,
            "retention_days": 90,
        },
    )
    assert dealership_response.status_code == 201
    dealership_id = dealership_response.json()["id"]

    location_response = client.post(
        "/api/v1/locations",
        headers=headers,
        json={"name": "Hamburg", "dealership_id": dealership_id},
    )

    assert location_response.status_code == 201
    assert location_response.json()["dealership_id"] == dealership_id


def test_repeated_vin_creates_incremented_job_version() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        db_dealership = db.get(Dealership, dealership.id)
        assert db_dealership is not None
        db_dealership.auto_export_enabled = True
        location = Location(dealership_id=dealership.id, name="Hauptstandort")
        db.add(location)
        db.commit()
        db.refresh(location)
        location_id = location.id

    tokens = login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    payload = {
        "location_id": str(location_id),
        "vin": " wvw123abc ",
        "brand": "Volkswagen",
    }

    first = client.post("/api/v1/jobs", headers=headers, json=payload)
    second = client.post("/api/v1/jobs", headers=headers, json=payload)

    assert first.status_code == 201
    assert first.json()["vin"] == "WVW123ABC"
    assert first.json()["version"] == 1
    assert first.json()["auto_export"] is True
    assert second.status_code == 201
    assert second.json()["version"] == 2


def test_dealership_user_cannot_use_location_from_other_dealership() -> None:
    create_dealership_admin()
    with TestingSession() as db:
        other_dealership = Dealership(name="Fremdes Autohaus")
        db.add(other_dealership)
        db.flush()
        other_location = Location(dealership_id=other_dealership.id, name="Fremdstandort")
        db.add(other_location)
        db.commit()
        db.refresh(other_location)
        other_location_id = other_location.id

    tokens = login()
    response = client.post(
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={
            "location_id": str(other_location_id),
            "vin": "VIN-ISOLATION",
            "brand": "Test",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Standort wurde nicht gefunden"


def test_dealership_job_list_is_tenant_scoped() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        own_location = Location(dealership_id=dealership.id, name="Eigener Standort")
        db.add(own_location)
        other_dealership = Dealership(name="Anderer Mandant")
        db.add(other_dealership)
        db.flush()
        other_location = Location(dealership_id=other_dealership.id, name="Anderer Standort")
        db.add(other_location)
        db.commit()
        db.refresh(own_location)
        own_location_id = own_location.id

    tokens = login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    created = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"location_id": str(own_location_id), "vin": "OWN-VIN", "brand": "Marke"},
    )
    assert created.status_code == 201

    response = client.get("/api/v1/jobs", headers=headers)

    assert response.status_code == 200
    assert [job["vin"] for job in response.json()] == ["OWN-VIN"]


def test_admin_interface_requires_login() -> None:
    response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_dealership_admin_logs_into_tenant_interface() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        db.add_all(
            [
                Location(dealership_id=dealership.id, name="Bad Neustadt"),
                Brand(dealership_id=dealership.id, name="Ford"),
            ]
        )
        db.commit()
    login_page = client.get("/admin/login")

    login_response = client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
        follow_redirects=False,
    )
    dashboard = client.get("/admin")

    assert login_response.status_code == 303
    assert dashboard.status_code == 200
    assert dealership.name in dashboard.text
    assert "Aktueller Credit-Stand" in dashboard.text
    assert "Auftrag manuell anlegen" in dashboard.text
    assert 'id="create-job-dialog"' in dashboard.text
    assert "Standorte und Benutzer" not in dashboard.text
    assert "SFTP-Übertragung" not in dashboard.text
    assert "Autohaus hinzufügen" not in dashboard.text


def test_dealership_admin_only_opens_limited_photo_configuration() -> None:
    dealership, _ = create_dealership_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )

    detail_response = client.get(f"/admin/dealerships/{dealership.id}")
    configuration_response = client.get(f"/admin/dealerships/{dealership.id}/configuration")
    create_location_response = client.post(
        f"/admin/dealerships/{dealership.id}/locations",
        data={"name": "Nicht erlaubt", "csrf_token": "irrelevant"},
    )

    assert detail_response.status_code == 403
    assert configuration_response.status_code == 200
    assert 'id="orientations"' in configuration_response.text
    assert 'id="overlays"' in configuration_response.text
    assert 'id="supplemental-images"' in configuration_response.text
    assert 'id="brands"' not in configuration_response.text
    assert 'id="backgrounds"' not in configuration_response.text
    assert 'id="capture-steps"' not in configuration_response.text
    assert create_location_response.status_code == 403
    with TestingSession() as db:
        assert db.scalar(select(Location).where(Location.name == "Nicht erlaubt")) is None


def test_dealership_admin_configures_separate_app_and_export_orders() -> None:
    dealership, _ = create_dealership_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    configuration_page = client.get(f"/admin/dealerships/{dealership.id}/configuration")
    assert 'data-sequence="capture"' in configuration_page.text
    assert 'data-sequence="export"' in configuration_page.text
    assert 'type="hidden" name="capture_orders"' in configuration_page.text
    assert 'type="hidden" name="export_orders"' in configuration_page.text
    assert 'type="number" name="capture_orders"' not in configuration_page.text
    assert 'type="number" name="export_orders"' not in configuration_page.text
    with TestingSession() as db:
        orientation = db.scalar(select(Orientation).where(Orientation.name == "Vorne links"))
        assert orientation is not None
        orientation_id = orientation.id

    response = client.post(
        f"/admin/dealerships/{dealership.id}/orientation-settings",
        data={
            "orientation_ids": str(orientation_id),
            "capture_orders": "7",
            "export_orders": "2",
            "required_orientation_ids": str(orientation_id),
            "active_orientation_ids": str(orientation_id),
            "csrf_token": csrf_from(configuration_page.text),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "App- und Exportreihenfolge wurden gespeichert" in response.text
    with TestingSession() as db:
        step = db.scalar(
            select(CaptureStep).where(
                CaptureStep.dealership_id == dealership.id,
                CaptureStep.orientation_id == orientation_id,
            )
        )
        assert step is not None
        assert step.capture_order == 7
        assert step.export_order == 2
        assert step.is_active is True


def test_dealership_admin_configures_repeatable_orientation_instances() -> None:
    dealership, _ = create_dealership_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    configuration_page = client.get(f"/admin/dealerships/{dealership.id}/configuration")
    with TestingSession() as db:
        orientation = db.scalar(select(Orientation).where(Orientation.key == "infotainment"))
        assert orientation is not None
        orientation_id = orientation.id

    response = client.post(
        f"/admin/dealerships/{dealership.id}/orientation-settings",
        data={
            "orientation_ids": str(orientation_id),
            "capture_orders": "10",
            "export_orders": "20",
            "instance_counts": "3",
            "required_orientation_ids": str(orientation_id),
            "active_orientation_ids": str(orientation_id),
            "csrf_token": csrf_from(configuration_page.text),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "App- und Exportreihenfolge wurden gespeichert" in response.text
    with TestingSession() as db:
        steps = list(
            db.scalars(
                select(CaptureStep)
                .where(
                    CaptureStep.dealership_id == dealership.id,
                    CaptureStep.orientation_id == orientation_id,
                    CaptureStep.is_active.is_(True),
                )
                .order_by(CaptureStep.orientation_instance_index)
            )
        )
        assert [step.name for step in steps] == [
            "Navigation/Infotainment 1",
            "Navigation/Infotainment 2",
            "Navigation/Infotainment 3",
        ]
        assert [step.capture_order for step in steps] == [10, 11, 12]
        assert [step.export_order for step in steps] == [20, 21, 22]

    reduced_response = client.post(
        f"/admin/dealerships/{dealership.id}/orientation-settings",
        data={
            "orientation_ids": str(orientation_id),
            "capture_orders": "4",
            "export_orders": "8",
            "instance_counts": "1",
            "active_orientation_ids": str(orientation_id),
            "csrf_token": csrf_from(response.text),
        },
        follow_redirects=True,
    )

    assert reduced_response.status_code == 200
    with TestingSession() as db:
        all_steps = list(
            db.scalars(
                select(CaptureStep)
                .where(
                    CaptureStep.dealership_id == dealership.id,
                    CaptureStep.orientation_id == orientation_id,
                )
                .order_by(CaptureStep.orientation_instance_index)
            )
        )
        assert [step.is_active for step in all_steps] == [True, False, False]
        assert all_steps[0].capture_order == 4
        assert all_steps[0].export_order == 8


def test_dealership_admin_cannot_manage_central_orientation_catalog() -> None:
    create_dealership_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )

    response = client.get("/admin/orientations")

    assert response.status_code == 403


def test_system_admin_sees_grouped_orientation_catalog_with_default_silhouettes() -> None:
    create_system_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "system@example.com",
            "password": "a-secure-system-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )

    response = client.get("/admin/orientations")

    assert response.status_code == 200
    assert 'id="category-exterior"' in response.text
    assert 'id="category-interior"' in response.text
    assert 'id="category-detail"' in response.text
    assert 'id="category-special"' in response.text
    assert "/admin/static/orientation-silhouettes/front.png" in response.text
    assert "System-Silhouette" in response.text
    assert 'value="window_background"' in response.text
    assert "Scheibenhintergrund" in response.text


def test_admin_form_rejects_invalid_csrf_token() -> None:
    create_dealership_admin()

    response = client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": "invalid",
        },
    )

    assert response.status_code == 400


def test_photographer_cannot_log_into_admin_interface() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        photographer = User(
            dealership_id=dealership.id,
            email="photo@example.com",
            password_hash=hash_password("a-secure-photo-password"),
            role=UserRole.PHOTOGRAPHER,
        )
        db.add(photographer)
        db.commit()
    login_page = client.get("/admin/login")

    response = client.post(
        "/admin/login",
        data={
            "email": "photo@example.com",
            "password": "a-secure-photo-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )

    assert response.status_code == 401
    assert "E-Mail-Adresse oder Passwort ist nicht korrekt" in response.text


def test_operator_can_only_use_web_quality_review() -> None:
    create_operator()

    api_response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "operator@example.com",
            "password": "a-secure-operator-password",
        },
    )
    assert api_response.status_code == 401

    login_page = client.get("/admin/login")
    login_response = client.post(
        "/admin/login",
        data={
            "email": "operator@example.com",
            "password": "a-secure-operator-password",
            "csrf_token": csrf_from(login_page.text),
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/admin"
    dashboard = client.get("/admin", follow_redirects=False)
    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/admin/quality-reviews"
    quality_page = client.get("/admin/quality-reviews")
    assert quality_page.status_code == 200
    assert 'id="quality-review-content"' in quality_page.text
    assert client.get("/admin/orientations").status_code == 403


def test_admin_interface_rejects_invalid_user_email() -> None:
    dealership, _ = create_dealership_admin()
    create_system_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "system@example.com",
            "password": "a-secure-system-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    detail_page = client.get(f"/admin/dealerships/{dealership.id}")

    response = client.post(
        f"/admin/dealerships/{dealership.id}/users",
        data={
            "email": "keine-adresse",
            "password": "another-secure-password",
            "role": "photographer",
            "csrf_token": csrf_from(detail_page.text),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Bitte geben Sie eine gültige E-Mail-Adresse ein" in response.text
    with TestingSession() as db:
        assert db.scalar(select(User).where(User.email == "keine-adresse")) is None


def test_admin_interface_rejects_duplicate_location() -> None:
    dealership, _ = create_dealership_admin()
    create_system_admin()
    with TestingSession() as db:
        db.add(Location(dealership_id=dealership.id, name="Bad Neustadt"))
        db.commit()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "system@example.com",
            "password": "a-secure-system-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    detail_page = client.get(f"/admin/dealerships/{dealership.id}")

    response = client.post(
        f"/admin/dealerships/{dealership.id}/locations",
        data={
            "name": "bad neustadt",
            "csrf_token": csrf_from(detail_page.text),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Dieser Standort ist bereits vorhanden" in response.text
    with TestingSession() as db:
        locations = list(db.scalars(select(Location)))
        assert len(locations) == 1


def test_inactive_dealership_cannot_log_in() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        stored_dealership = db.get(Dealership, dealership.id)
        assert stored_dealership is not None
        stored_dealership.is_active = False
        db.commit()

    api_response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "a-secure-test-password"},
    )
    login_page = client.get("/admin/login")
    admin_response = client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )

    assert api_response.status_code == 401
    assert admin_response.status_code == 401


def test_admin_adds_brand_and_standard_capture_sequence() -> None:
    dealership, _ = create_dealership_admin()
    create_system_admin()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "system@example.com",
            "password": "a-secure-system-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    configuration_page = client.get(f"/admin/dealerships/{dealership.id}/configuration")
    csrf_token = csrf_from(configuration_page.text)

    brand_response = client.post(
        f"/admin/dealerships/{dealership.id}/brands",
        data={"name": "Volkswagen", "csrf_token": csrf_token},
        follow_redirects=True,
    )
    defaults_response = client.post(
        f"/admin/dealerships/{dealership.id}/capture-steps/defaults",
        data={"csrf_token": csrf_from(brand_response.text)},
        follow_redirects=True,
    )

    assert brand_response.status_code == 200
    assert "Volkswagen" in brand_response.text
    assert defaults_response.status_code == 200
    assert "36 Standard-Fotopositionen wurden ergänzt" in defaults_response.text
    with TestingSession() as db:
        brands = list(db.scalars(select(Brand)))
        steps = list(db.scalars(select(CaptureStep).order_by(CaptureStep.capture_order)))
        assert [brand.name for brand in brands] == ["Volkswagen"]
        assert len(steps) == 36
        assert steps[0].name == "Vorne"
        assert steps[0].requires_processing is True
        steering_wheel = db.scalar(
            select(Orientation).where(Orientation.key == "steering-wheel")
        )
        assert steering_wheel is not None
        assert steering_wheel.processing_mode == "window_background"
        assert steps[-1].name == "Spezialaufnahme 1"
        assert steps[-1].requires_processing is False


def test_admin_uploads_background_with_location_assignment() -> None:
    dealership, _ = create_dealership_admin()
    create_system_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        brand = Brand(dealership_id=dealership.id, name="Volkswagen")
        db.add_all([location, brand])
        db.commit()
        db.refresh(location)
        db.refresh(brand)
        location_id = location.id
        brand_id = brand.id
    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        login_page = client.get("/admin/login")
        client.post(
            "/admin/login",
            data={
                "email": "system@example.com",
                "password": "a-secure-system-password",
                "csrf_token": csrf_from(login_page.text),
            },
        )
        configuration_page = client.get(f"/admin/dealerships/{dealership.id}/configuration")

        response = client.post(
            f"/admin/dealerships/{dealership.id}/backgrounds",
            data={
                "name": "Heller Showroom",
                "brand_id": str(brand_id),
                "location_ids": str(location_id),
                "csrf_token": csrf_from(configuration_page.text),
            },
            files={"image": ("showroom.jpg", b"\xff\xd8\xffimage-content", "image/jpeg")},
            follow_redirects=True,
        )
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert response.status_code == 200
    assert "Hintergrund wurde hochgeladen" in response.text
    assert len(storage.uploads) == 1
    assert storage.uploads[0]["content_type"] == "image/jpeg"
    with TestingSession() as db:
        background = db.scalar(select(Background).options(selectinload(Background.locations)))
        assert background is not None
        assert background.brand_id == brand_id
        assert [item.id for item in background.locations] == [location_id]


def test_system_admin_configures_background_defaults_and_orientation_override() -> None:
    dealership, _ = create_dealership_admin()
    create_system_admin()
    with TestingSession() as db:
        background = Background(
            dealership_id=dealership.id,
            name="Standard",
            object_key="configuration/standard.jpg",
            content_type="image/jpeg",
        )
        db.add(background)
        db.commit()
        background_id = background.id

    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        login_page = client.get("/admin/login")
        client.post(
            "/admin/login",
            data={
                "email": "system@example.com",
                "password": "a-secure-system-password",
                "csrf_token": csrf_from(login_page.text),
            },
        )
        configuration_page = client.get(f"/admin/dealerships/{dealership.id}/configuration")
        with TestingSession() as db:
            orientation = db.scalar(select(Orientation).where(Orientation.key == "front-right"))
            assert orientation is not None
            orientation_id = orientation.id

        response = client.post(
            f"/admin/backgrounds/{background_id}",
            data={
                "name": "Standard",
                "contour_target_area_percent": "38",
                "contour_max_width_percent": "80",
                "contour_max_height_percent": "74",
                "vehicle_bottom_percent": "91",
                "shadow_opacity_percent": "36",
                "reflection_opacity_percent": "8",
                "brightness_percent": "102",
                "window_background_shift_percent": "18",
                "scene_horizon_percent": "43",
                "scene_reference_vertical_degrees": "0",
                "scene_perspective_strength_percent": "35",
                "composition_orientation_ids": str(orientation_id),
                "custom_composition_orientation_ids": str(orientation_id),
                "orientation_target_area_percents": "",
                "orientation_max_width_percents": "",
                "orientation_max_height_percents": "",
                "orientation_bottom_percents": "94",
                "orientation_shadow_percents": "48",
                "orientation_reflection_percents": "",
                "orientation_brightness_percents": "",
                "orientation_window_shift_percents": "22",
                "is_active": "on",
                "csrf_token": csrf_from(configuration_page.text),
            },
            follow_redirects=True,
        )
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert response.status_code == 200
    assert "Hintergrund wurde gespeichert" in response.text
    assert "Standard-Bildkomposition" in response.text
    assert "Abweichungen je Orientierung" in response.text
    assert 'aria-label="Information anzeigen"' in response.text
    with TestingSession() as db:
        background = db.get(Background, background_id)
        override = db.scalar(
            select(BackgroundOrientationComposition).where(
                BackgroundOrientationComposition.background_id == background_id,
                BackgroundOrientationComposition.orientation_id == orientation_id,
            )
        )
        assert background is not None
        assert background.contour_target_area_percent == 38
        assert background.shadow_opacity_percent == 36
        assert background.window_background_shift_percent == 18
        assert override is not None
        assert override.vehicle_bottom_percent == 94
        assert override.shadow_opacity_percent == 48
        assert override.window_background_shift_percent == 22
        assert override.contour_target_area_percent is None
        assert override.brightness_percent is None


def test_system_admin_manages_tenant_overlay_and_supplemental_image() -> None:
    dealership, _ = create_dealership_admin()
    create_system_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        brand = Brand(dealership_id=dealership.id, name="Ford")
        step = CaptureStep(
            dealership_id=dealership.id,
            name="Front",
            instruction="Gerade aufnehmen",
            category="exterior",
            capture_order=1,
            export_order=1,
            requires_processing=True,
        )
        db.add_all([location, brand, step])
        db.commit()
        location_id = location.id
        brand_id = brand.id
        step_id = step.id
    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        login_page = client.get("/admin/login")
        client.post(
            "/admin/login",
            data={
                "email": "system@example.com",
                "password": "a-secure-system-password",
                "csrf_token": csrf_from(login_page.text),
            },
        )
        configuration_page = client.get(f"/admin/dealerships/{dealership.id}/configuration")
        overlay_response = client.post(
            f"/admin/dealerships/{dealership.id}/overlays",
            data={
                "name": "Autohauslogo",
                "brand_id": str(brand_id),
                "location_ids": str(location_id),
                "position": "bottom_right",
                "width_percent": "18",
                "opacity_percent": "90",
                "csrf_token": csrf_from(configuration_page.text),
            },
            files={"image": ("logo.png", b"\x89PNG\r\n\x1a\ncontent", "image/png")},
            follow_redirects=True,
        )
        conflict_response = client.post(
            f"/admin/dealerships/{dealership.id}/supplemental-images",
            data={
                "name": "Kollision",
                "export_order": "1",
                "brand_id": str(brand_id),
                "location_ids": str(location_id),
                "csrf_token": csrf_from(overlay_response.text),
            },
            files={"image": ("collision.jpg", b"\xff\xd8\xffcontent", "image/jpeg")},
            follow_redirects=True,
        )
        supplemental_response = client.post(
            f"/admin/dealerships/{dealership.id}/supplemental-images",
            data={
                "name": "Garantie",
                "export_order": "20",
                "brand_id": str(brand_id),
                "location_ids": str(location_id),
                "csrf_token": csrf_from(conflict_response.text),
            },
            files={"image": ("garantie.jpg", b"\xff\xd8\xffcontent", "image/jpeg")},
            follow_redirects=True,
        )
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert overlay_response.status_code == 200
    assert "Overlay wurde hochgeladen" in overlay_response.text
    assert "Zusatzbild wurde hochgeladen" in conflict_response.text
    assert supplemental_response.status_code == 200
    assert "Zusatzbild wurde hochgeladen" in supplemental_response.text
    assert len(storage.uploads) == 3
    with TestingSession() as db:
        overlay = db.scalar(
            select(ImageOverlay).options(
                selectinload(ImageOverlay.locations),
                selectinload(ImageOverlay.capture_steps),
            )
        )
        supplemental = db.scalar(
            select(SupplementalImage)
            .options(selectinload(SupplementalImage.locations))
            .where(SupplementalImage.name == "Garantie")
        )
        assert overlay is not None
        assert overlay.dealership_id == dealership.id
        assert overlay.brand_id == brand_id
        assert overlay.position == "bottom_right"
        assert overlay.width_percent == 18
        assert overlay.opacity_percent == 90
        assert [item.id for item in overlay.locations] == [location_id]
        assert [item.id for item in overlay.capture_steps] == [step_id]
        assert supplemental is not None
        assert supplemental.dealership_id == dealership.id
        assert supplemental.brand_id == brand_id
        assert supplemental.export_order == 20
        assert [item.id for item in supplemental.locations] == [location_id]


def test_system_admin_permanently_deletes_configuration_images() -> None:
    dealership, _ = create_dealership_admin()
    create_system_admin()
    with TestingSession() as db:
        background = Background(
            dealership_id=dealership.id,
            name="Alter Hintergrund",
            object_key="configuration/background.jpg",
            content_type="image/jpeg",
        )
        overlay = ImageOverlay(
            dealership_id=dealership.id,
            name="Altes Logo",
            object_key="configuration/overlay.png",
            content_type="image/png",
        )
        supplemental = SupplementalImage(
            dealership_id=dealership.id,
            name="Alte Werbung",
            object_key="configuration/supplemental.jpg",
            content_type="image/jpeg",
            export_order=20,
        )
        db.add_all([background, overlay, supplemental])
        db.commit()
        background_id = background.id
        overlay_id = overlay.id
        supplemental_id = supplemental.id

    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        login_page = client.get("/admin/login")
        client.post(
            "/admin/login",
            data={
                "email": "system@example.com",
                "password": "a-secure-system-password",
                "csrf_token": csrf_from(login_page.text),
            },
        )
        configuration_page = client.get(f"/admin/dealerships/{dealership.id}/configuration")
        csrf_token = csrf_from(configuration_page.text)

        background_response = client.post(
            f"/admin/backgrounds/{background_id}/delete",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        overlay_response = client.post(
            f"/admin/overlays/{overlay_id}/delete",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        supplemental_response = client.post(
            f"/admin/supplemental-images/{supplemental_id}/delete",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert "Hintergrund wurde dauerhaft gelöscht" in background_response.text
    assert "Overlay wurde dauerhaft gelöscht" in overlay_response.text
    assert "Zusatzbild wurde dauerhaft gelöscht" in supplemental_response.text
    assert storage.deleted_keys == [
        "configuration/background.jpg",
        "configuration/overlay.png",
        "configuration/supplemental.jpg",
    ]
    with TestingSession() as db:
        assert db.get(Background, background_id) is None
        assert db.get(ImageOverlay, overlay_id) is None
        assert db.get(SupplementalImage, supplemental_id) is None


def test_background_used_by_vehicle_job_is_unlinked_and_deleted() -> None:
    dealership, admin = create_dealership_admin()
    create_system_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        background = Background(
            dealership_id=dealership.id,
            name="Verwendeter Hintergrund",
            object_key="configuration/in-use.jpg",
            content_type="image/jpeg",
        )
        db.add_all([location, background])
        db.flush()
        job = VehicleJob(
            dealership_id=dealership.id,
            location_id=location.id,
            created_by_id=admin.id,
            vin="TESTVIN",
            version=1,
            brand="Ford",
            background_id=background.id,
        )
        db.add(job)
        db.commit()
        background_id = background.id
        job_id = job.id

    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        login_page = client.get("/admin/login")
        client.post(
            "/admin/login",
            data={
                "email": "system@example.com",
                "password": "a-secure-system-password",
                "csrf_token": csrf_from(login_page.text),
            },
        )
        configuration_page = client.get(f"/admin/dealerships/{dealership.id}/configuration")
        response = client.post(
            f"/admin/backgrounds/{background_id}/delete",
            data={"csrf_token": csrf_from(configuration_page.text)},
            follow_redirects=True,
        )
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert "Hintergrund wurde dauerhaft gelöscht" in response.text
    assert storage.deleted_keys == ["configuration/in-use.jpg"]
    with TestingSession() as db:
        assert db.get(Background, background_id) is None
        job = db.get(VehicleJob, job_id)
        assert job is not None
        assert job.background_id is None


def test_app_configuration_is_location_and_tenant_scoped() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        own_location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        other_location = Location(dealership_id=dealership.id, name="Bad Kissingen")
        brand = Brand(dealership_id=dealership.id, name="Volkswagen")
        orientation = Orientation(
            key="front",
            name="Vorne",
            instruction="Fahrzeug gerade aufnehmen",
            category="exterior",
            default_capture_order=1,
        )
        db.add_all([own_location, other_location, brand, orientation])
        db.flush()
        db.add_all(
            [
                Background(
                    dealership_id=dealership.id,
                    brand_id=brand.id,
                    name="Für Neustadt",
                    object_key="backgrounds/neustadt.jpg",
                    content_type="image/jpeg",
                    locations=[own_location],
                ),
                Background(
                    dealership_id=dealership.id,
                    brand_id=None,
                    name="Für alle",
                    object_key="backgrounds/all.jpg",
                    content_type="image/jpeg",
                ),
                Background(
                    dealership_id=dealership.id,
                    brand_id=brand.id,
                    name="Für Kissingen",
                    object_key="backgrounds/kissingen.jpg",
                    content_type="image/jpeg",
                    locations=[other_location],
                ),
                CaptureStep(
                    dealership_id=dealership.id,
                    orientation_id=orientation.id,
                    name="Front",
                    instruction="Gerade aufnehmen",
                    category="exterior",
                    capture_order=1,
                    export_order=3,
                    requires_processing=True,
                ),
            ]
        )
        db.commit()
        db.refresh(own_location)
        own_location_id = own_location.id
    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        tokens = login()
        response = client.get(
            "/api/v1/configuration",
            params={"location_id": str(own_location_id)},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert response.status_code == 200
    payload = response.json()
    assert [brand["name"] for brand in payload["brands"]] == ["Volkswagen"]
    assert {background["name"] for background in payload["backgrounds"]} == {
        "Für alle",
        "Für Neustadt",
    }
    assert payload["capture_steps"][0]["capture_order"] == 1
    assert payload["capture_steps"][0]["export_order"] == 3
    assert payload["capture_steps"][0]["requires_processing"] is True
    assert payload["capture_steps"][0]["silhouette_url"].endswith(
        "/admin/static/orientation-silhouettes/front.png"
    )


def test_job_stores_selected_brand_and_background() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        brand = Brand(dealership_id=dealership.id, name="Volkswagen")
        db.add_all([location, brand])
        db.flush()
        background = Background(
            dealership_id=dealership.id,
            brand_id=brand.id,
            name="Showroom",
            object_key="backgrounds/showroom.jpg",
            content_type="image/jpeg",
            locations=[location],
        )
        db.add(background)
        db.commit()
        db.refresh(location)
        db.refresh(brand)
        db.refresh(background)
        location_id = location.id
        brand_id = brand.id
        background_id = background.id

    tokens = login()
    response = client.post(
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={
            "location_id": str(location_id),
            "vin": "CONFIG-VIN",
            "brand": "Wird serverseitig ersetzt",
            "brand_id": str(brand_id),
            "background_id": str(background_id),
        },
    )

    assert response.status_code == 201
    assert response.json()["brand"] == "Volkswagen"
    assert response.json()["brand_id"] == str(brand_id)
    assert response.json()["background_id"] == str(background_id)


def test_admin_creates_manual_job_and_uploads_benchmark_photo() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        brand = Brand(dealership_id=dealership.id, name="Ford")
        step = CaptureStep(
            dealership_id=dealership.id,
            name="Diagonal vorne links",
            instruction="Vordere linke Fahrzeugecke zeigen",
            category="exterior",
            capture_order=2,
            export_order=2,
            requires_processing=True,
        )
        db.add_all([location, brand, step])
        db.flush()
        background = Background(
            dealership_id=dealership.id,
            brand_id=brand.id,
            name="Showroom",
            object_key="backgrounds/manual-showroom.jpg",
            content_type="image/jpeg",
            locations=[location],
        )
        db.add(background)
        db.commit()
        db.refresh(location)
        db.refresh(brand)
        db.refresh(background)
        db.refresh(step)
        location_id = location.id
        brand_id = brand.id
        background_id = background.id
        step_id = step.id

    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    jobs_page = client.get(f"/admin/dealerships/{dealership.id}/jobs")
    assert 'id="job-list-section"' in jobs_page.text
    assert "data-live-refresh" in jobs_page.text
    assert "admin-live-refresh.js" in jobs_page.text
    created = client.post(
        f"/admin/dealerships/{dealership.id}/jobs",
        data={
            "vin": " manual-test-vin ",
            "location_id": str(location_id),
            "brand_id": str(brand_id),
            "background_id": str(background_id),
            "csrf_token": csrf_from(jobs_page.text),
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    with TestingSession() as db:
        job = db.scalar(select(VehicleJob).where(VehicleJob.vin == "MANUAL-TEST-VIN"))
        assert job is not None
        assert job.auto_export is False
        assert job.brand == "Ford"
        assert job.background_id == background_id
        job_id = job.id

    detail_page = client.get(f"/admin/jobs/{job_id}")
    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        uploaded = client.post(
            f"/admin/jobs/{job_id}/photos",
            data={
                "capture_step_id": str(step_id),
                "csrf_token": csrf_from(detail_page.text),
            },
            files={
                "original_image": ("original.jpg", jpeg_bytes("navy"), "image/jpeg"),
                "benchmark_image": ("reference.jpg", jpeg_bytes("silver"), "image/jpeg"),
            },
            follow_redirects=False,
        )
        rendered = client.get(f"/admin/jobs/{job_id}")
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert uploaded.status_code == 303
    assert len(storage.uploads) == 4
    assert "Referenzergebnis" in rendered.text
    assert 'id="job-status-heading"' in rendered.text
    assert 'id="job-export-status"' in rendered.text
    assert 'id="job-photo-review"' in rendered.text
    assert "data-live-version" in rendered.text
    with TestingSession() as db:
        photo = db.scalar(select(PhotoAsset).where(PhotoAsset.vehicle_job_id == job_id))
        assert photo is not None
        assert photo.original_thumbnail_object_key is not None
        assert photo.benchmark_object_key is not None
        assert photo.benchmark_thumbnail_object_key is not None
        assert photo.processing_status == ProcessingStatus.PENDING


def test_job_list_uses_optimized_front_left_photo_as_thumbnail() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        step = CaptureStep(
            dealership_id=dealership.id,
            name="Diagonal vorne links",
            instruction="Vordere linke Fahrzeugecke zeigen",
            category="exterior",
            capture_order=2,
            export_order=2,
            requires_processing=False,
        )
        db.add_all([location, step])
        db.commit()
        db.refresh(location)
        db.refresh(step)
        location_id = location.id
        step_id = step.id

    tokens = login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    job_response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"location_id": str(location_id), "vin": "THUMB-VIN", "brand": "Ford"},
    )
    job_id = job_response.json()["id"]
    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        upload = client.post(
            f"/api/v1/jobs/{job_id}/capture/uploads",
            headers=headers,
            json={
                "capture_step_id": str(step_id),
                "content_type": "image/jpeg",
                "size_bytes": 1234,
            },
        )
        assert upload.status_code == 201
        completed = client.post(
            f"/api/v1/jobs/{job_id}/capture/photos/{upload.json()['photo_id']}/complete",
            headers=headers,
        )
        assert completed.status_code == 200

        with TestingSession() as db:
            photo = db.get(PhotoAsset, uuid.UUID(upload.json()["photo_id"]))
            assert photo is not None
            photo.processed_object_key = "processed/thumbnail-optimized.jpg"
            photo.processed_thumbnail_object_key = "processed/thumbnail-optimized.thumbnail.jpg"
            db.commit()

        response = client.get("/api/v1/jobs", headers=headers)

        assert response.status_code == 200
        assert response.json()[0]["thumbnail_url"].startswith(
            "https://images.example/processed/thumbnail-optimized.thumbnail.jpg"
        )
    finally:
        app.dependency_overrides.pop(get_object_storage, None)


def test_guided_capture_upload_tracks_progress_and_revision() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        step = CaptureStep(
            dealership_id=dealership.id,
            name="Front",
            instruction="Gerade aufnehmen",
            category="exterior",
            capture_order=1,
            export_order=1,
            requires_processing=True,
        )
        db.add_all([location, step])
        db.commit()
        db.refresh(location)
        db.refresh(step)
        location_id = location.id
        step_id = step.id
    tokens = login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    job_response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"location_id": str(location_id), "vin": "PHOTO-VIN", "brand": "Ford"},
    )
    job_id = job_response.json()["id"]
    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        initial_session = client.get(f"/api/v1/jobs/{job_id}/capture", headers=headers)
        first_upload = client.post(
            f"/api/v1/jobs/{job_id}/capture/uploads",
            headers=headers,
            json={
                "capture_step_id": str(step_id),
                "content_type": "image/jpeg",
                "size_bytes": 1234,
                "capture_metadata": {
                    "horizon_angle_degrees": 1.5,
                    "vertical_angle_degrees": -3.0,
                    "yaw_angle_degrees": 12.0,
                    "field_of_view_degrees": 68.0,
                    "motion_available": True,
                },
            },
        )
        first_complete = client.post(
            f"/api/v1/jobs/{job_id}/capture/photos/{first_upload.json()['photo_id']}/complete",
            headers=headers,
        )
        second_upload = client.post(
            f"/api/v1/jobs/{job_id}/capture/uploads",
            headers=headers,
            json={
                "capture_step_id": str(step_id),
                "content_type": "image/jpeg",
                "size_bytes": 1234,
            },
        )
        second_complete = client.post(
            f"/api/v1/jobs/{job_id}/capture/photos/{second_upload.json()['photo_id']}/complete",
            headers=headers,
        )
        final_session = client.get(f"/api/v1/jobs/{job_id}/capture", headers=headers)
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert initial_session.status_code == 200
    assert len(initial_session.json()["capture_steps"]) == 1
    assert initial_session.json()["photos"] == []
    assert first_upload.status_code == 201
    assert first_upload.json()["revision"] == 1
    with TestingSession() as db:
        stored_photo = db.get(PhotoAsset, uuid.UUID(first_upload.json()["photo_id"]))
        assert stored_photo is not None
        assert stored_photo.capture_metadata == {
            "horizon_angle_degrees": 1.5,
            "vertical_angle_degrees": -3.0,
            "yaw_angle_degrees": 12.0,
            "field_of_view_degrees": 68.0,
            "motion_available": True,
        }
    assert first_complete.status_code == 200
    assert first_complete.json()["thumbnail_url"].endswith(".thumbnail.jpg?expires=900")
    assert second_upload.json()["revision"] == 2
    assert second_complete.status_code == 200
    assert len(final_session.json()["photos"]) == 1
    assert final_session.json()["photos"][0]["revision"] == 2
    assert final_session.json()["photos"][0]["thumbnail_url"].endswith(".thumbnail.jpg?expires=900")
    assert any(str(upload["object_key"]).endswith(".thumbnail.jpg") for upload in storage.uploads)


def test_capture_must_be_completed_and_is_locked_afterwards() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        required_step = CaptureStep(
            dealership_id=dealership.id,
            name="Front",
            instruction="Gerade aufnehmen",
            category="exterior",
            capture_order=1,
            export_order=1,
            is_required=True,
            requires_processing=False,
        )
        optional_step = CaptureStep(
            dealership_id=dealership.id,
            name="Detail",
            instruction="Optionales Detail",
            category="detail",
            capture_order=2,
            export_order=2,
            is_required=False,
            requires_processing=False,
        )
        db.add_all([location, required_step, optional_step])
        db.commit()
        db.refresh(location)
        db.refresh(required_step)
        db.refresh(optional_step)
        location_id = location.id
        required_step_id = required_step.id
        optional_step_id = optional_step.id

    tokens = login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    job_response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"location_id": str(location_id), "vin": "FINISH-VIN", "brand": "Ford"},
    )
    job_id = job_response.json()["id"]

    missing = client.post(f"/api/v1/jobs/{job_id}/capture/complete", headers=headers)
    assert missing.status_code == 409
    assert missing.json()["detail"] == "Pflichtfotos fehlen: Front"

    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        upload = client.post(
            f"/api/v1/jobs/{job_id}/capture/uploads",
            headers=headers,
            json={
                "capture_step_id": str(required_step_id),
                "content_type": "image/jpeg",
                "size_bytes": 1234,
            },
        )
        completed_photo = client.post(
            f"/api/v1/jobs/{job_id}/capture/photos/{upload.json()['photo_id']}/complete",
            headers=headers,
        )
        completed_capture = client.post(f"/api/v1/jobs/{job_id}/capture/complete", headers=headers)
        blocked_upload = client.post(
            f"/api/v1/jobs/{job_id}/capture/uploads",
            headers=headers,
            json={
                "capture_step_id": str(optional_step_id),
                "content_type": "image/jpeg",
                "size_bytes": 1234,
            },
        )
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert completed_photo.status_code == 200
    assert completed_capture.status_code == 200
    assert completed_capture.json()["capture_completed_at"] is not None
    assert blocked_upload.status_code == 409
    assert blocked_upload.json()["detail"] == "Die Aufnahme wurde bereits abgeschlossen"


def test_auto_export_waits_for_explicit_capture_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    dealership, user = create_dealership_admin()
    queued_exports: list[uuid.UUID] = []
    monkeypatch.setattr(
        "app.processing_queue.enqueue_vehicle_export",
        lambda export_run_id: queued_exports.append(export_run_id),
    )
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        step = CaptureStep(
            dealership_id=dealership.id,
            name="Front",
            instruction="Gerade aufnehmen",
            category="exterior",
            capture_order=1,
            export_order=1,
            is_required=True,
            requires_processing=False,
        )
        db.add_all([location, step])
        db.flush()
        job = VehicleJob(
            dealership_id=dealership.id,
            location_id=location.id,
            created_by_id=user.id,
            vin="AUTO-EXPORT-VIN",
            version=1,
            brand="Ford",
            auto_export=True,
        )
        db.add(job)
        db.flush()
        db.add(
            PhotoAsset(
                vehicle_job_id=job.id,
                capture_step_id=step.id,
                captured_by_id=user.id,
                revision=1,
                original_object_key="originals/auto-export.jpg",
                original_content_type="image/jpeg",
                expected_size_bytes=1234,
                original_size_bytes=1234,
                uploaded_at=datetime.now(timezone.utc),
                is_selected=True,
                processing_status=ProcessingStatus.NOT_REQUIRED,
            )
        )
        db.commit()

        try_enqueue_auto_export(job.id, db)
        assert db.scalar(select(ExportRun)) is None
        assert queued_exports == []

        job.capture_completed_at = datetime.now(timezone.utc)
        db.commit()
        try_enqueue_auto_export(job.id, db)

        export_run = db.scalar(select(ExportRun))
        assert export_run is not None
        assert queued_exports == [export_run.id]


def test_dealership_admin_can_only_submit_processed_photo_for_improvement() -> None:
    dealership, user = create_dealership_admin()
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        step = CaptureStep(
            dealership_id=dealership.id,
            name="Lenkrad",
            instruction="Lenkrad mittig aufnehmen",
            category="interior",
            capture_order=1,
            export_order=1,
            is_required=True,
            requires_processing=True,
        )
        db.add_all([location, step])
        db.flush()
        job = VehicleJob(
            dealership_id=dealership.id,
            location_id=location.id,
            created_by_id=user.id,
            vin="IMPROVEMENT-VIN",
            version=1,
            brand="Ford",
            status=JobStatus.COMPLETED,
        )
        db.add(job)
        db.flush()
        photo = PhotoAsset(
            vehicle_job_id=job.id,
            capture_step_id=step.id,
            captured_by_id=user.id,
            revision=1,
            original_object_key="originals/improvement.jpg",
            original_content_type="image/jpeg",
            expected_size_bytes=1234,
            original_size_bytes=1234,
            uploaded_at=datetime.now(timezone.utc),
            is_selected=True,
            processed_object_key="processed/improvement.jpg",
            processed_content_type="image/jpeg",
            processed_size_bytes=1234,
            processing_status=ProcessingStatus.COMPLETED,
        )
        db.add(photo)
        db.commit()
        job_id = job.id
        photo_id = photo.id

    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    storage = ConfigurationStorage()
    app.dependency_overrides[get_object_storage] = lambda: storage
    try:
        detail_page = client.get(f"/admin/jobs/{job_id}")
        assert detail_page.status_code == 200
        assert "Zur Verbesserung vorlegen" in detail_page.text
        assert "Scheibenmaske nachbearbeiten" not in detail_page.text
        assert ">Verarbeitung starten<" not in detail_page.text

        submitted = client.post(
            f"/admin/photos/{photo_id}/request-improvement",
            data={"csrf_token": csrf_from(detail_page.text)},
            follow_redirects=False,
        )
        correction_page = client.get(f"/admin/photos/{photo_id}/correction")
    finally:
        app.dependency_overrides.pop(get_object_storage, None)

    assert submitted.status_code == 303
    assert submitted.headers["location"] == f"/admin/jobs/{job_id}"
    assert correction_page.status_code == 403
    with TestingSession() as db:
        submitted_photo = db.get(PhotoAsset, photo_id)
        submitted_job = db.get(VehicleJob, job_id)
        assert submitted_photo is not None
        assert submitted_job is not None
        assert submitted_photo.quality_review_required is True
        assert submitted_photo.quality_review_resolution == "requested_by_dealership"
        assert submitted_photo.quality_review_created_at is not None
        assert submitted_job.status == JobStatus.REVIEW_REQUIRED


def test_auto_export_resumes_after_last_quality_review_is_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dealership, user = create_dealership_admin()
    create_system_admin()
    queued_exports: list[uuid.UUID] = []
    monkeypatch.setattr(
        "app.processing_queue.enqueue_vehicle_export",
        lambda export_run_id: queued_exports.append(export_run_id),
    )
    with TestingSession() as db:
        location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        step = CaptureStep(
            dealership_id=dealership.id,
            name="Front",
            instruction="Gerade aufnehmen",
            category="exterior",
            capture_order=1,
            export_order=1,
            is_required=True,
            requires_processing=True,
        )
        db.add_all([location, step])
        db.flush()
        job = VehicleJob(
            dealership_id=dealership.id,
            location_id=location.id,
            created_by_id=user.id,
            vin="QUALITY-EXPORT-VIN",
            version=1,
            brand="Ford",
            auto_export=True,
            capture_completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.flush()
        photo = PhotoAsset(
            vehicle_job_id=job.id,
            capture_step_id=step.id,
            captured_by_id=user.id,
            revision=1,
            original_object_key="originals/quality-export.jpg",
            original_content_type="image/jpeg",
            expected_size_bytes=1234,
            original_size_bytes=1234,
            uploaded_at=datetime.now(timezone.utc),
            is_selected=True,
            processed_object_key="processed/quality-export.jpg",
            processed_content_type="image/jpeg",
            processed_size_bytes=1234,
            processing_status=ProcessingStatus.COMPLETED,
            quality_review_required=True,
            quality_review_reason="Automatische Prüfung auffällig",
        )
        db.add(photo)
        db.commit()
        photo_id = photo.id
        job_id = job.id

        try_enqueue_auto_export(job_id, db)
        assert db.scalar(select(ExportRun)) is None
        assert queued_exports == []

    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "system@example.com",
            "password": "a-secure-system-password",
            "csrf_token": csrf_from(login_page.text),
        },
    )
    review_page = client.get("/admin/quality-reviews")
    response = client.post(
        f"/admin/quality-reviews/{photo_id}/approve",
        data={"csrf_token": csrf_from(review_page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with TestingSession() as db:
        approved_photo = db.get(PhotoAsset, photo_id)
        export_run = db.scalar(select(ExportRun))
        assert approved_photo is not None
        assert approved_photo.quality_review_required is False
        assert approved_photo.quality_review_resolution == "approved"
        assert export_run is not None
        assert queued_exports == [export_run.id]


def test_guided_capture_is_tenant_scoped() -> None:
    create_dealership_admin()
    with TestingSession() as db:
        other_dealership = Dealership(name="Fremdes Autohaus")
        db.add(other_dealership)
        db.flush()
        other_location = Location(dealership_id=other_dealership.id, name="Fremdstandort")
        other_user = User(
            dealership_id=other_dealership.id,
            email="other@example.com",
            password_hash=hash_password("another-secure-password"),
            role=UserRole.DEALERSHIP_ADMIN,
        )
        db.add_all([other_location, other_user])
        db.flush()
        other_job = VehicleJob(
            dealership_id=other_dealership.id,
            location_id=other_location.id,
            created_by_id=other_user.id,
            vin="OTHER-VIN",
            version=1,
            brand="Andere Marke",
        )
        db.add(other_job)
        db.commit()
        db.refresh(other_job)
        other_job_id = other_job.id

    tokens = login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = client.get(f"/api/v1/jobs/{other_job_id}/capture", headers=headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Auftrag wurde nicht gefunden"
