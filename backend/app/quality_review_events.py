import logging

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PhotoAsset

logger = logging.getLogger(__name__)
_INFO_KEY = "quality_review_notification_ids"


@event.listens_for(Session, "before_flush")
def collect_new_quality_reviews(session: Session, *_: object) -> None:
    photo_ids = session.info.setdefault(_INFO_KEY, set())
    for candidate in session.dirty:
        if not isinstance(candidate, PhotoAsset) or not candidate.quality_review_required:
            continue
        state = inspect(candidate)
        entered_review = state.attrs.quality_review_required.history.has_changes()
        restarted_review = state.attrs.quality_review_created_at.history.has_changes()
        if candidate.id is not None and (entered_review or restarted_review):
            photo_ids.add(candidate.id)


@event.listens_for(Session, "after_commit")
def enqueue_new_quality_reviews(session: Session) -> None:
    photo_ids = session.info.pop(_INFO_KEY, set())
    if not photo_ids or not get_settings().web_push_enabled:
        return
    from app.processing_queue import enqueue_quality_review_notification

    for photo_id in photo_ids:
        try:
            enqueue_quality_review_notification(photo_id)
        except Exception:
            logger.exception("Could not enqueue quality review notification for %s", photo_id)


@event.listens_for(Session, "after_rollback")
def clear_new_quality_reviews(session: Session) -> None:
    session.info.pop(_INFO_KEY, None)
