from fastapi.testclient import TestClient

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
