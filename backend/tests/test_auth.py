import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Dealership, User, UserRole
from app.security import hash_password

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
