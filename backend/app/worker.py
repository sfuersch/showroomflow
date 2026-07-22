import logging
import uuid
from datetime import datetime, timezone

from redis import Redis
from rq import Queue, Worker

from app.database import SessionLocal
from app.models import JobStatus, PhotoAsset, ProcessingStatus, VehicleJob
from app.config import get_settings
from app import quality_review_events as _quality_review_events  # noqa: F401

logger = logging.getLogger(__name__)


def handle_work_horse_killed(job, retpid, ret_val, rusage) -> None:
    """Persist an actionable status when RQ cannot return control to the job."""
    if job.func_name != "app.processing.process_photo" or not job.args:
        return
    try:
        photo_id = uuid.UUID(str(job.args[0]))
    except (TypeError, ValueError):
        logger.warning("Killed image job %s has no valid photo id", job.id)
        return

    error = (
        "Die Bildverarbeitung wurde unerwartet beendet. Wahrscheinlich war das "
        "Bild für den verfügbaren Arbeitsspeicher zu groß."
    )
    with SessionLocal() as db:
        photo = db.get(PhotoAsset, photo_id)
        if photo is None:
            return
        photo.processing_status = ProcessingStatus.FAILED
        photo.processing_error = error
        photo.quality_review_required = True
        photo.quality_review_reason = error
        photo.quality_score = 0
        photo.quality_issues = [error]
        photo.quality_model_version = "processing-health-v1"
        if photo.quality_review_created_at is None:
            photo.quality_review_created_at = datetime.now(timezone.utc)
        photo.quality_reviewed_by_id = None
        photo.quality_reviewed_at = None
        photo.quality_review_resolution = None
        vehicle_job = db.get(VehicleJob, photo.vehicle_job_id)
        if vehicle_job is not None:
            vehicle_job.status = JobStatus.REVIEW_REQUIRED
        db.commit()


def main() -> None:
    settings = get_settings()
    connection = Redis.from_url(settings.redis_url)
    worker = Worker(
        [Queue(settings.processing_queue, connection=connection)],
        connection=connection,
        work_horse_killed_handler=handle_work_horse_killed,
    )
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
