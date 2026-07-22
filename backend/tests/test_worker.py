import uuid
from types import SimpleNamespace

from app.models import JobStatus, PhotoAsset, ProcessingStatus, VehicleJob
from app.worker import handle_work_horse_killed


def test_killed_photo_worker_persists_failed_review_status(monkeypatch) -> None:
    photo_id = uuid.uuid4()
    vehicle_job_id = uuid.uuid4()
    photo = SimpleNamespace(
        vehicle_job_id=vehicle_job_id,
        processing_status=ProcessingStatus.PROCESSING,
        processing_error=None,
        quality_review_required=True,
        quality_review_reason=None,
        quality_score=100,
        quality_issues=[],
        quality_model_version=None,
        quality_review_created_at=None,
        quality_reviewed_by_id=uuid.uuid4(),
        quality_reviewed_at=object(),
        quality_review_resolution="correction_processing",
    )
    vehicle_job = SimpleNamespace(status=JobStatus.PROCESSING)

    class FakeSession:
        committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, model, identifier):
            if model is PhotoAsset and identifier == photo_id:
                return photo
            if model is VehicleJob and identifier == vehicle_job_id:
                return vehicle_job
            return None

        def commit(self):
            self.committed = True

    session = FakeSession()
    monkeypatch.setattr("app.worker.SessionLocal", lambda: session)
    rq_job = SimpleNamespace(
        id="photo-job",
        func_name="app.processing.process_photo",
        args=(str(photo_id),),
    )

    handle_work_horse_killed(rq_job, 10, 9, None)

    assert session.committed is True
    assert photo.processing_status == ProcessingStatus.FAILED
    assert "Arbeitsspeicher" in photo.processing_error
    assert photo.quality_review_required is True
    assert photo.quality_review_resolution is None
    assert vehicle_job.status == JobStatus.REVIEW_REQUIRED
