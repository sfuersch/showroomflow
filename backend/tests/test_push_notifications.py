import uuid

from app.push_notifications import quality_review_payload


def test_quality_review_payload_links_directly_to_review() -> None:
    photo_id = uuid.uuid4()
    payload = quality_review_payload(
        photo_id=photo_id,
        dealership_name="Test Autohaus",
        vin="TESTVIN123",
        step_name="Vorne links",
        base_url="https://showroomflow.example/",
    )

    assert payload["title"] == "Neues Bild in der Qualitätsprüfung"
    assert payload["body"] == "Test Autohaus · TESTVIN123 · Vorne links"
    assert payload["url"] == (
        f"https://showroomflow.example/admin/quality-reviews#quality-review-{photo_id}"
    )
