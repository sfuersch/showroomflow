from redis import Redis
from rq import Queue, Worker

from app.config import get_settings
from app import quality_review_events as _quality_review_events  # noqa: F401


def main() -> None:
    settings = get_settings()
    connection = Redis.from_url(settings.redis_url)
    worker = Worker(
        [Queue(settings.processing_queue, connection=connection)], connection=connection
    )
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
