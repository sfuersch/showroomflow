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
    Brand,
    CaptureStep,
    Dealership,
    ExportRun,
    ImageOverlay,
    Location,
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

    settings_response = client.post(
        "/admin/image-service",
        data={
            "provider": "photoroom",
            "default_monthly_vehicle_credits": "40",
            "contour_target_area_percent": "37",
            "contour_max_width_percent": "79",
            "contour_max_height_percent": "73",
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
        assert image_settings.contour_target_area_percent == 37
        assert image_settings.contour_max_width_percent == 79
        assert image_settings.contour_max_height_percent == 73
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
    assert "Autohaus hinzufügen" not in dashboard.text


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


def test_admin_interface_rejects_invalid_user_email() -> None:
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
    with TestingSession() as db:
        db.add(Location(dealership_id=dealership.id, name="Bad Neustadt"))
        db.commit()
    login_page = client.get("/admin/login")
    client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "a-secure-test-password",
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
    assert "16 Standard-Fotopositionen wurden ergänzt" in defaults_response.text
    with TestingSession() as db:
        brands = list(db.scalars(select(Brand)))
        steps = list(db.scalars(select(CaptureStep).order_by(CaptureStep.capture_order)))
        assert [brand.name for brand in brands] == ["Volkswagen"]
        assert len(steps) == 16
        assert steps[0].name == "Front"
        assert steps[0].requires_processing is True
        assert steps[-1].name == "Kofferraum"
        assert steps[-1].requires_processing is False


def test_admin_uploads_background_with_location_assignment() -> None:
    dealership, _ = create_dealership_admin()
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
                "email": "admin@example.com",
                "password": "a-secure-test-password",
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


def test_dealership_admin_manages_tenant_overlay_and_supplemental_image() -> None:
    dealership, _ = create_dealership_admin()
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
                "email": "admin@example.com",
                "password": "a-secure-test-password",
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
    assert "Exportplatz 1 ist bereits" in conflict_response.text
    assert supplemental_response.status_code == 200
    assert "Zusatzbild wurde hochgeladen" in supplemental_response.text
    assert len(storage.uploads) == 2
    with TestingSession() as db:
        overlay = db.scalar(
            select(ImageOverlay).options(
                selectinload(ImageOverlay.locations),
                selectinload(ImageOverlay.capture_steps),
            )
        )
        supplemental = db.scalar(
            select(SupplementalImage).options(selectinload(SupplementalImage.locations))
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


def test_app_configuration_is_location_and_tenant_scoped() -> None:
    dealership, _ = create_dealership_admin()
    with TestingSession() as db:
        own_location = Location(dealership_id=dealership.id, name="Bad Neustadt")
        other_location = Location(dealership_id=dealership.id, name="Bad Kissingen")
        brand = Brand(dealership_id=dealership.id, name="Volkswagen")
        db.add_all([own_location, other_location, brand])
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
    assert first_complete.status_code == 200
    assert first_complete.json()["thumbnail_url"].endswith(".thumbnail.jpg?expires=900")
    assert second_upload.json()["revision"] == 2
    assert second_complete.status_code == 200
    assert len(final_session.json()["photos"]) == 1
    assert final_session.json()["photos"][0]["revision"] == 2
    assert final_session.json()["photos"][0]["thumbnail_url"].endswith(
        ".thumbnail.jpg?expires=900"
    )
    assert any(
        str(upload["object_key"]).endswith(".thumbnail.jpg") for upload in storage.uploads
    )


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
        completed_capture = client.post(
            f"/api/v1/jobs/{job_id}/capture/complete", headers=headers
        )
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
