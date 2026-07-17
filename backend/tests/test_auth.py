import uuid
from collections.abc import Generator
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Background, Brand, CaptureStep, Dealership, Location, User, UserRole
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

    def create_download_url(self, *, object_key: str, expires_in: int = 900) -> str:
        return f"https://images.example/{object_key}?expires={expires_in}"


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
