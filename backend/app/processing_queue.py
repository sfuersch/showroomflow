import uuid

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, Retry

from app.config import get_settings
from app.exporting import process_export_run
from app.processing import process_photo, process_photo_variant


class ProcessingQueueUnavailable(RuntimeError):
    """The image-processing queue cannot accept work."""


def enqueue_photo_processing(photo_id: uuid.UUID) -> None:
    settings = get_settings()
    try:
        connection = Redis.from_url(settings.redis_url)
        queue = Queue(settings.processing_queue, connection=connection)
        queue.enqueue(
            process_photo,
            str(photo_id),
            job_id=f"photo-{photo_id}-{uuid.uuid4()}",
            job_timeout=300,
            retry=Retry(max=3, interval=[30, 120, 300]),
            result_ttl=86400,
            failure_ttl=604800,
        )
    except RedisError as exc:
        raise ProcessingQueueUnavailable("Processing queue is unavailable") from exc


def enqueue_photo_variant(photo_id: uuid.UUID, provider: str) -> None:
    settings = get_settings()
    try:
        connection = Redis.from_url(settings.redis_url)
        queue = Queue(settings.processing_queue, connection=connection)
        queue.enqueue(
            process_photo_variant,
            str(photo_id),
            provider,
            job_id=f"photo-{photo_id}-{provider}-{uuid.uuid4()}",
            job_timeout=300,
            retry=Retry(max=3, interval=[30, 120, 300]),
            result_ttl=86400,
            failure_ttl=604800,
        )
    except RedisError as exc:
        raise ProcessingQueueUnavailable("Processing queue is unavailable") from exc


def enqueue_vehicle_export(export_run_id: uuid.UUID) -> None:
    settings = get_settings()
    try:
        connection = Redis.from_url(settings.redis_url)
        queue = Queue(settings.processing_queue, connection=connection)
        queue.enqueue(
            process_export_run,
            str(export_run_id),
            job_id=f"export-{export_run_id}-{uuid.uuid4()}",
            job_timeout=600,
            retry=Retry(max=2, interval=[60, 300]),
            result_ttl=86400,
            failure_ttl=604800,
        )
    except RedisError as exc:
        raise ProcessingQueueUnavailable("Processing queue is unavailable") from exc
