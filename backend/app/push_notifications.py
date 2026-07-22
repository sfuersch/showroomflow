import json
import logging
import uuid
from datetime import datetime, timezone

from pywebpush import WebPushException, webpush
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    CaptureStep,
    Dealership,
    PhotoAsset,
    User,
    UserRole,
    VehicleJob,
    WebPushSubscription,
)

logger = logging.getLogger(__name__)


class PushDeliveryError(RuntimeError):
    """No active browser received a Web Push notification."""


def quality_review_payload(
    *, photo_id: uuid.UUID, dealership_name: str, vin: str, step_name: str, base_url: str
) -> dict[str, str]:
    return {
        "title": "Neues Bild in der Qualitätsprüfung",
        "body": f"{dealership_name} · {vin} · {step_name}",
        "url": f"{base_url.rstrip('/')}/admin/quality-reviews#quality-review-{photo_id}",
        "tag": f"quality-review-{photo_id}",
    }


def _status_code(exc: WebPushException) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def send_quality_review_notification(photo_id: str) -> None:
    settings = get_settings()
    if not settings.web_push_enabled:
        return

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = db.execute(
            select(PhotoAsset, VehicleJob, CaptureStep, Dealership)
            .join(VehicleJob, VehicleJob.id == PhotoAsset.vehicle_job_id)
            .join(CaptureStep, CaptureStep.id == PhotoAsset.capture_step_id)
            .join(Dealership, Dealership.id == VehicleJob.dealership_id)
            .where(PhotoAsset.id == uuid.UUID(photo_id))
        ).one_or_none()
        if row is None:
            return
        photo, job, step, dealership = row
        review_created_at = photo.quality_review_created_at
        if not photo.quality_review_required or review_created_at is None:
            return
        if (
            photo.quality_review_notified_at is not None
            and photo.quality_review_notified_at >= review_created_at
        ):
            return

        subscriptions = list(
            db.scalars(
                select(WebPushSubscription)
                .join(User, User.id == WebPushSubscription.user_id)
                .where(
                    WebPushSubscription.is_active.is_(True),
                    User.is_active.is_(True),
                    User.role.in_([UserRole.SYSTEM_ADMIN, UserRole.OPERATOR]),
                )
            )
        )
        if not subscriptions:
            return

        payload = quality_review_payload(
            photo_id=photo.id,
            dealership_name=dealership.name,
            vin=job.vin,
            step_name=step.name,
            base_url=settings.public_base_url,
        )
        delivered = False
        transient_failure = False
        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info={
                        "endpoint": subscription.endpoint,
                        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                    },
                    data=json.dumps(payload),
                    vapid_private_key=settings.web_push_vapid_private_key,
                    vapid_claims={"sub": settings.web_push_subject},
                    ttl=86400,
                )
            except WebPushException as exc:
                subscription.failure_count += 1
                subscription.last_failure_at = now
                if _status_code(exc) in {404, 410}:
                    subscription.is_active = False
                else:
                    transient_failure = True
                logger.warning(
                    "Web Push delivery failed for subscription %s (HTTP %s)",
                    subscription.id,
                    _status_code(exc),
                )
            else:
                delivered = True
                subscription.failure_count = 0
                subscription.last_success_at = now

        if delivered:
            photo.quality_review_notified_at = now
        db.commit()
        if not delivered and transient_failure:
            raise PushDeliveryError("Web Push delivery failed temporarily")
