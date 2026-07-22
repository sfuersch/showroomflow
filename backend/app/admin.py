import io
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from hmac import compare_digest
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from PIL import Image
from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.exporting import (
    ExportValidationError,
    resolve_export_items,
    safe_vin,
    try_enqueue_auto_export,
)
from app.image_service import (
    IMAGE_PROVIDERS,
    VehicleCreditsExhausted,
    credit_balance,
    grant_additional_credits,
    get_image_settings,
    photoroom_sandbox_active,
    provider_is_available,
    reserve_vehicle_credit,
)
from app.models import (
    Background,
    BackgroundOrientationComposition,
    Brand,
    CaptureStep,
    Dealership,
    DealershipSftpSettings,
    ExportRun,
    ExternalApiUsage,
    ImageOverlay,
    JobStatus,
    Location,
    Orientation,
    PhotoAsset,
    PhotoProcessingVariant,
    ProcessingStatus,
    RefreshSession,
    SupplementalImage,
    User,
    UserRole,
    VehicleCreditGrant,
    VehicleJob,
    WebPushSubscription,
)
from app.orientations import (
    MASKED_BACKGROUND_MODES,
    ORIENTATION_CATEGORIES,
    PROCESSING_MODES,
    PROCESSING_REQUIRED_MODES,
    STANDARD_ORIENTATIONS,
    default_silhouette_path,
    instance_name,
)
from app.processing_queue import (
    ProcessingQueueUnavailable,
    enqueue_export_transfer,
    enqueue_photo_processing,
    enqueue_photo_variant,
    enqueue_quality_review_notification,
    enqueue_vehicle_export,
)
from app.security import hash_password, verify_password
from app.sftp_transfer import (
    SftpConfigurationError,
    encrypt_password,
    fetch_host_key_fingerprint,
    normalize_fingerprint,
    normalize_remote_directory,
    test_sftp_connection,
    validate_settings as validate_sftp_settings,
)
from app.storage import ObjectStorage, StorageUnavailableError, get_object_storage
from app.thumbnails import ThumbnailError, create_thumbnail, thumbnail_key

router = APIRouter(prefix="/admin", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
PUSH_SERVICE_WORKER_PATH = Path(__file__).parent / "static" / "admin-push-service-worker.js"

MAX_CONFIGURATION_IMAGE_BYTES = 20 * 1024 * 1024
MAX_JOB_PHOTO_BYTES = 30 * 1024 * 1024
IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png"}
OVERLAY_POSITIONS = {
    "top_left": "Oben links",
    "top_right": "Oben rechts",
    "bottom_left": "Unten links",
    "bottom_right": "Unten rechts",
    "center": "Mittig",
}


class PushKeysPayload(BaseModel):
    p256dh: str = Field(min_length=20, max_length=255)
    auth: str = Field(min_length=8, max_length=255)


class PushSubscriptionPayload(BaseModel):
    csrf_token: str
    endpoint: str = Field(min_length=20, max_length=4000)
    keys: PushKeysPayload


class PushRemovalPayload(BaseModel):
    csrf_token: str
    endpoint: str = Field(min_length=20, max_length=4000)


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _validate_csrf(request: Request, submitted_token: str) -> None:
    expected_token = request.session.get("csrf_token")
    if (
        not isinstance(expected_token, str)
        or not submitted_token
        or not compare_digest(expected_token, submitted_token)
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ungültige Anfrage")


def _current_admin(request: Request, db: Session) -> User | None:
    raw_user_id = request.session.get("admin_user_id")
    if not isinstance(raw_user_id, str):
        return None
    try:
        user_id = uuid.UUID(raw_user_id)
    except ValueError:
        request.session.clear()
        return None
    user = db.get(User, user_id)
    if (
        user is None
        or not user.is_active
        or user.role
        not in {UserRole.SYSTEM_ADMIN, UserRole.OPERATOR, UserRole.DEALERSHIP_ADMIN}
    ):
        request.session.clear()
        return None
    if user.dealership_id is not None:
        dealership = db.get(Dealership, user.dealership_id)
        if dealership is None or not dealership.is_active:
            request.session.clear()
            return None
    return user


def _require_admin(request: Request, db: Session) -> User | RedirectResponse:
    user = _current_admin(request, db)
    if user is None:
        return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    return user


def _require_system_admin(request: Request, db: Session) -> User | RedirectResponse:
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    return user


def _require_quality_operator(request: Request, db: Session) -> User | RedirectResponse:
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.role not in {UserRole.SYSTEM_ADMIN, UserRole.OPERATOR}:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    return user


def _flash(request: Request, message: str, category: str = "success") -> None:
    request.session["flash"] = {"message": message, "category": category}


def _context(request: Request, current_user: User | None = None, **values: object) -> dict:
    return {
        "request": request,
        "current_user": current_user,
        "csrf_token": _csrf_token(request),
        "flash": request.session.pop("flash", None),
        **values,
    }


def _authorized_dealership(db: Session, user: User, dealership_id: uuid.UUID) -> Dealership:
    dealership = db.get(Dealership, dealership_id)
    if dealership is None:
        raise HTTPException(status_code=404, detail="Autohaus wurde nicht gefunden")
    if user.role != UserRole.SYSTEM_ADMIN and user.dealership_id != dealership.id:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    return dealership


def _normalized_email(value: str) -> str | None:
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def _dealership_name_exists(
    db: Session, name: str, *, excluding_id: uuid.UUID | None = None
) -> bool:
    statement = select(Dealership.id).where(func.lower(Dealership.name) == name.lower())
    if excluding_id is not None:
        statement = statement.where(Dealership.id != excluding_id)
    return db.scalar(statement) is not None


def _location_name_exists(
    db: Session,
    dealership_id: uuid.UUID,
    name: str,
    *,
    excluding_id: uuid.UUID | None = None,
) -> bool:
    statement = select(Location.id).where(
        Location.dealership_id == dealership_id,
        func.lower(Location.name) == name.lower(),
    )
    if excluding_id is not None:
        statement = statement.where(Location.id != excluding_id)
    return db.scalar(statement) is not None


def _optional_uuid(value: str) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ungültige Auswahl") from exc


def _tenant_locations(
    db: Session, dealership_id: uuid.UUID, ids: list[uuid.UUID]
) -> list[Location]:
    if not ids:
        return []
    unique_ids = set(ids)
    locations = list(
        db.scalars(
            select(Location).where(
                Location.dealership_id == dealership_id,
                Location.id.in_(unique_ids),
            )
        )
    )
    if len(locations) != len(unique_ids):
        raise HTTPException(status_code=400, detail="Ungültige Standortauswahl")
    return locations


def _tenant_capture_steps(
    db: Session, dealership_id: uuid.UUID, ids: list[uuid.UUID]
) -> list[CaptureStep]:
    if not ids:
        return []
    unique_ids = set(ids)
    steps = list(
        db.scalars(
            select(CaptureStep).where(
                CaptureStep.dealership_id == dealership_id,
                CaptureStep.id.in_(unique_ids),
            )
        )
    )
    if len(steps) != len(unique_ids):
        raise HTTPException(status_code=400, detail="Ungültige Fotopositionsauswahl")
    return steps


def _overlay_capture_steps(
    db: Session, dealership_id: uuid.UUID, ids: list[uuid.UUID]
) -> list[CaptureStep]:
    selected_steps = _tenant_capture_steps(db, dealership_id, ids)
    if selected_steps:
        return selected_steps
    first_export_step = db.scalar(
        select(CaptureStep)
        .where(
            CaptureStep.dealership_id == dealership_id,
            CaptureStep.export_order.is_not(None),
            CaptureStep.is_active.is_(True),
        )
        .order_by(CaptureStep.export_order, CaptureStep.capture_order, CaptureStep.name)
        .limit(1)
    )
    return [first_export_step] if first_export_step else []


def _tenant_brand(
    db: Session, dealership_id: uuid.UUID, brand_id: uuid.UUID | None
) -> Brand | None:
    if brand_id is None:
        return None
    brand = db.get(Brand, brand_id)
    if brand is None or brand.dealership_id != dealership_id:
        raise HTTPException(status_code=400, detail="Ungültige Markenauswahl")
    return brand


def _capture_export_order_conflict(
    db: Session,
    dealership_id: uuid.UUID,
    export_order: int | None,
    *,
    excluding_step_id: uuid.UUID | None = None,
) -> str | None:
    if export_order is None:
        return None
    step_statement = select(CaptureStep).where(
        CaptureStep.dealership_id == dealership_id,
        CaptureStep.export_order == export_order,
        CaptureStep.is_active.is_(True),
    )
    if excluding_step_id is not None:
        step_statement = step_statement.where(CaptureStep.id != excluding_step_id)
    step = db.scalar(step_statement)
    return step.name if step is not None else None


def _capture_order_conflict(
    db: Session,
    dealership_id: uuid.UUID,
    capture_order: int,
    *,
    excluding_step_id: uuid.UUID | None = None,
) -> str | None:
    statement = select(CaptureStep).where(
        CaptureStep.dealership_id == dealership_id,
        CaptureStep.capture_order == capture_order,
        CaptureStep.is_active.is_(True),
    )
    if excluding_step_id is not None:
        statement = statement.where(CaptureStep.id != excluding_step_id)
    step = db.scalar(statement)
    return step.name if step is not None else None


def _ensure_standard_orientations(db: Session) -> list[Orientation]:
    orientations = list(db.scalars(select(Orientation).order_by(Orientation.default_capture_order)))
    existing_keys = {orientation.key for orientation in orientations}
    changed = False
    for position, standard in enumerate(STANDARD_ORIENTATIONS, start=1):
        if standard.key in existing_keys:
            orientation = next(item for item in orientations if item.key == standard.key)
            if not orientation.instruction:
                orientation.instruction = standard.instruction
                changed = True
            continue
        db.add(
            Orientation(
                key=standard.key,
                name=standard.name,
                instruction=standard.instruction,
                category=standard.category,
                default_capture_order=position,
                default_export_order=position,
                is_required=standard.required,
                requires_processing=standard.requires_processing,
                processing_mode=standard.processing_mode,
                is_repeatable=standard.repeatable,
                default_instance_count=standard.default_instances,
                max_instances=standard.max_instances,
            )
        )
        changed = True
    if changed:
        db.commit()
        orientations = list(
            db.scalars(select(Orientation).order_by(Orientation.default_capture_order))
        )
    return orientations


def _supplemental_export_order_conflict(
    db: Session,
    dealership_id: uuid.UUID,
    export_order: int,
    brand_id: uuid.UUID | None,
    locations: list[Location],
    *,
    excluding_image_id: uuid.UUID | None = None,
) -> str | None:
    statement = (
        select(SupplementalImage)
        .options(selectinload(SupplementalImage.locations))
        .where(
            SupplementalImage.dealership_id == dealership_id,
            SupplementalImage.export_order == export_order,
            SupplementalImage.is_active.is_(True),
        )
    )
    if excluding_image_id is not None:
        statement = statement.where(SupplementalImage.id != excluding_image_id)
    location_ids = {location.id for location in locations}
    for other in db.scalars(statement):
        brands_overlap = brand_id is None or other.brand_id is None or brand_id == other.brand_id
        other_location_ids = {location.id for location in other.locations}
        locations_overlap = (
            not location_ids or not other_location_ids or bool(location_ids & other_location_ids)
        )
        if brands_overlap and locations_overlap:
            return other.name
    return None


async def _store_configuration_image(
    storage: ObjectStorage,
    upload: UploadFile,
    *,
    object_key_prefix: str,
    png_only: bool = False,
) -> tuple[str, str]:
    content_type = upload.content_type or ""
    allowed_types = {"image/png"} if png_only else set(IMAGE_EXTENSIONS)
    if content_type not in allowed_types:
        raise HTTPException(
            status_code=400, detail="Bitte eine passende PNG- oder JPG-Datei wählen."
        )
    content = await upload.read(MAX_CONFIGURATION_IMAGE_BYTES + 1)
    if not content or len(content) > MAX_CONFIGURATION_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Die Bilddatei darf höchstens 20 MB groß sein.")
    valid_signature = (
        content.startswith(b"\x89PNG\r\n\x1a\n")
        if content_type == "image/png"
        else content.startswith(b"\xff\xd8\xff")
    )
    if not valid_signature:
        raise HTTPException(status_code=400, detail="Die Bilddatei ist beschädigt oder ungültig.")
    extension = IMAGE_EXTENSIONS[content_type]
    object_key = f"{object_key_prefix}/{uuid.uuid4()}.{extension}"
    try:
        storage.put_object(object_key=object_key, content=content, content_type=content_type)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Bildspeicher ist nicht erreichbar") from exc
    return object_key, content_type


async def _read_job_photo(upload: UploadFile) -> tuple[bytes, str, str]:
    content_type = upload.content_type or ""
    if content_type not in IMAGE_EXTENSIONS:
        raise ValueError("Bitte eine JPG- oder PNG-Datei wählen.")
    content = await upload.read(MAX_JOB_PHOTO_BYTES + 1)
    if not content or len(content) > MAX_JOB_PHOTO_BYTES:
        raise ValueError("Die Bilddatei darf höchstens 30 MB groß sein.")
    valid_signature = (
        content.startswith(b"\x89PNG\r\n\x1a\n")
        if content_type == "image/png"
        else content.startswith(b"\xff\xd8\xff")
    )
    if not valid_signature:
        raise ValueError("Die Bilddatei ist beschädigt oder ungültig.")
    return content, content_type, IMAGE_EXTENSIONS[content_type]


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if _current_admin(request, db) is not None:
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "admin/login.html", _context(request))


@router.post("/login")
def login(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    _validate_csrf(request, csrf_token)
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    dealership = db.get(Dealership, user.dealership_id) if user and user.dealership_id else None
    if (
        user is None
        or not user.is_active
        or user.role
        not in {UserRole.SYSTEM_ADMIN, UserRole.OPERATOR, UserRole.DEALERSHIP_ADMIN}
        or (user.dealership_id is not None and (dealership is None or not dealership.is_active))
        or not verify_password(password, user.password_hash)
    ):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            _context(request, error="E-Mail-Adresse oder Passwort ist nicht korrekt."),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    request.session.clear()
    request.session["admin_user_id"] = str(user.id)
    _csrf_token(request)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form()):
    _validate_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    if admin.role == UserRole.DEALERSHIP_ADMIN:
        if admin.dealership_id is None:
            raise HTTPException(status_code=403, detail="Keine Berechtigung")
        return RedirectResponse(
            f"/admin/dealerships/{admin.dealership_id}/jobs",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if admin.role == UserRole.OPERATOR:
        return RedirectResponse(
            "/admin/quality-reviews", status_code=status.HTTP_303_SEE_OTHER
        )
    statement = select(Dealership).order_by(Dealership.name)
    dealerships = list(db.scalars(statement))
    credit_balances = {dealership.id: credit_balance(db, dealership) for dealership in dealerships}
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        _context(
            request,
            admin,
            dealerships=dealerships,
            credit_balances=credit_balances,
        ),
    )


@router.get("/dealerships", response_class=HTMLResponse)
def dealerships_page(request: Request, db: Session = Depends(get_db)):
    return dashboard(request, db)


@router.get("/api-usage", response_class=HTMLResponse)
def external_api_usage_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    dealership_id: str = "",
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin

    today = datetime.now(timezone.utc).date()
    try:
        start_date = date.fromisoformat(date_from) if date_from else today - timedelta(days=29)
        end_date = date.fromisoformat(date_to) if date_to else today
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ungültiger Zeitraum") from exc
    if end_date < start_date or (end_date - start_date).days > 366:
        raise HTTPException(status_code=400, detail="Ungültiger Zeitraum")
    start_at = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

    selected_dealership_id = _optional_uuid(dealership_id)
    dealerships = list(db.scalars(select(Dealership).order_by(Dealership.name)))
    if selected_dealership_id is not None and not any(
        item.id == selected_dealership_id for item in dealerships
    ):
        raise HTTPException(status_code=400, detail="Ungültiges Autohaus")

    base_filters = [
        ExternalApiUsage.occurred_at >= start_at,
        ExternalApiUsage.occurred_at < end_at,
    ]
    if selected_dealership_id is not None:
        base_filters.append(ExternalApiUsage.dealership_id == selected_dealership_id)
    paid_filters = [*base_filters, ExternalApiUsage.sandbox.is_(False)]

    paid_calls = db.scalar(
        select(func.count(ExternalApiUsage.id)).where(*paid_filters)
    ) or 0
    successful_calls = db.scalar(
        select(func.count(ExternalApiUsage.id)).where(
            *paid_filters, ExternalApiUsage.outcome == "success"
        )
    ) or 0
    throttled_calls = db.scalar(
        select(func.count(ExternalApiUsage.id)).where(
            *paid_filters, ExternalApiUsage.outcome == "throttled"
        )
    ) or 0
    failed_calls = paid_calls - successful_calls
    vehicle_count = db.scalar(
        select(func.count(func.distinct(ExternalApiUsage.vehicle_job_id))).where(
            *paid_filters
        )
    ) or 0
    sandbox_calls = db.scalar(
        select(func.count(ExternalApiUsage.id)).where(
            *base_filters, ExternalApiUsage.sandbox.is_(True)
        )
    ) or 0
    average_duration = db.scalar(
        select(func.avg(ExternalApiUsage.duration_ms)).where(*paid_filters)
    ) or 0

    dealership_rows = list(
        db.execute(
            select(
                Dealership.id,
                Dealership.name,
                func.count(ExternalApiUsage.id).label("calls"),
                func.count(func.distinct(ExternalApiUsage.vehicle_job_id)).label("vehicles"),
                func.sum(case((ExternalApiUsage.outcome == "success", 1), else_=0)).label(
                    "successes"
                ),
            )
            .join(ExternalApiUsage, ExternalApiUsage.dealership_id == Dealership.id)
            .where(*paid_filters)
            .group_by(Dealership.id, Dealership.name)
            .order_by(func.count(ExternalApiUsage.id).desc(), Dealership.name)
        )
    )
    vehicle_rows = list(
        db.execute(
            select(
                VehicleJob.id,
                VehicleJob.vin,
                VehicleJob.version,
                Dealership.id.label("dealership_id"),
                Dealership.name.label("dealership_name"),
                func.count(ExternalApiUsage.id).label("calls"),
                func.sum(case((ExternalApiUsage.outcome == "success", 1), else_=0)).label(
                    "successes"
                ),
                func.sum(case((ExternalApiUsage.outcome == "throttled", 1), else_=0)).label(
                    "throttled"
                ),
                func.max(ExternalApiUsage.occurred_at).label("last_call"),
            )
            .join(ExternalApiUsage, ExternalApiUsage.vehicle_job_id == VehicleJob.id)
            .join(Dealership, Dealership.id == VehicleJob.dealership_id)
            .where(*paid_filters)
            .group_by(
                VehicleJob.id,
                VehicleJob.vin,
                VehicleJob.version,
                Dealership.id,
                Dealership.name,
            )
            .order_by(func.count(ExternalApiUsage.id).desc(), VehicleJob.vin)
            .limit(100)
        )
    )
    operation_rows = list(
        db.execute(
            select(
                ExternalApiUsage.provider,
                ExternalApiUsage.operation,
                func.count(ExternalApiUsage.id).label("calls"),
                func.sum(case((ExternalApiUsage.outcome == "success", 1), else_=0)).label(
                    "successes"
                ),
                func.avg(ExternalApiUsage.duration_ms).label("average_duration"),
            )
            .where(*paid_filters)
            .group_by(ExternalApiUsage.provider, ExternalApiUsage.operation)
            .order_by(func.count(ExternalApiUsage.id).desc())
        )
    )

    return templates.TemplateResponse(
        request,
        "admin/api_usage.html",
        _context(
            request,
            admin,
            dealerships=dealerships,
            selected_dealership_id=selected_dealership_id,
            date_from=start_date.isoformat(),
            date_to=end_date.isoformat(),
            paid_calls=paid_calls,
            successful_calls=successful_calls,
            failed_calls=failed_calls,
            throttled_calls=throttled_calls,
            vehicle_count=vehicle_count,
            sandbox_calls=sandbox_calls,
            average_duration_ms=round(float(average_duration)),
            average_calls_per_vehicle=(round(paid_calls / vehicle_count, 1) if vehicle_count else 0),
            dealership_rows=dealership_rows,
            vehicle_rows=vehicle_rows,
            operation_rows=operation_rows,
            operation_names={
                "background_removal": "Freistellung",
                "contour_cutout": "Konturerkennung",
                "guided_segmentation": "Geführte Maskierung",
                "showroom_composition": "Showroom-Komposition",
            },
        ),
    )


@router.get("/push-service-worker.js", response_class=Response)
def push_service_worker() -> Response:
    return Response(
        PUSH_SERVICE_WORKER_PATH.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/admin/", "Cache-Control": "no-store"},
    )


@router.get("/manifest.webmanifest", response_class=JSONResponse)
def admin_manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "ShowroomFlow Qualitätsprüfung",
            "short_name": "ShowroomFlow",
            "start_url": "/admin/quality-reviews",
            "scope": "/admin/",
            "display": "standalone",
            "background_color": "#f6f7fb",
            "theme_color": "#5865f2",
        },
        media_type="application/manifest+json",
    )


@router.post("/push-subscriptions", response_class=JSONResponse)
def save_push_subscription(
    payload: PushSubscriptionPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    admin = _require_quality_operator(request, db)
    if isinstance(admin, RedirectResponse):
        raise HTTPException(status_code=401, detail="Anmeldung erforderlich")
    _validate_csrf(request, payload.csrf_token)
    if not get_settings().web_push_enabled:
        raise HTTPException(status_code=503, detail="Benachrichtigungen sind nicht konfiguriert")

    subscription = db.scalar(
        select(WebPushSubscription).where(WebPushSubscription.endpoint == payload.endpoint)
    )
    if subscription is None:
        subscription = WebPushSubscription(
            user_id=admin.id,
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
        )
        db.add(subscription)
    else:
        subscription.user_id = admin.id
        subscription.p256dh = payload.keys.p256dh
        subscription.auth = payload.keys.auth
    subscription.user_agent = request.headers.get("user-agent", "")[:500] or None
    subscription.is_active = True
    subscription.failure_count = 0
    db.commit()

    pending_photo_ids = list(
        db.scalars(
            select(PhotoAsset.id).where(
                PhotoAsset.quality_review_required.is_(True),
                PhotoAsset.quality_review_created_at.is_not(None),
                (
                    PhotoAsset.quality_review_notified_at.is_(None)
                    | (
                        PhotoAsset.quality_review_notified_at
                        < PhotoAsset.quality_review_created_at
                    )
                ),
            )
        )
    )
    for photo_id in pending_photo_ids:
        try:
            enqueue_quality_review_notification(photo_id)
        except ProcessingQueueUnavailable:
            pass
    return JSONResponse({"status": "ok"})


@router.post("/push-subscriptions/remove", response_class=JSONResponse)
def remove_push_subscription(
    payload: PushRemovalPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    admin = _require_quality_operator(request, db)
    if isinstance(admin, RedirectResponse):
        raise HTTPException(status_code=401, detail="Anmeldung erforderlich")
    _validate_csrf(request, payload.csrf_token)
    subscription = db.scalar(
        select(WebPushSubscription).where(
            WebPushSubscription.endpoint == payload.endpoint,
            WebPushSubscription.user_id == admin.id,
        )
    )
    if subscription is not None:
        db.delete(subscription)
        db.commit()
    return JSONResponse({"status": "ok"})


@router.get("/quality-reviews", response_class=HTMLResponse)
def quality_reviews_page(
    request: Request,
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_quality_operator(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    rows = list(
        db.execute(
            select(PhotoAsset, VehicleJob, CaptureStep, Dealership)
            .join(VehicleJob, VehicleJob.id == PhotoAsset.vehicle_job_id)
            .join(CaptureStep, CaptureStep.id == PhotoAsset.capture_step_id)
            .join(Dealership, Dealership.id == VehicleJob.dealership_id)
            .where(
                PhotoAsset.is_selected.is_(True),
                PhotoAsset.uploaded_at.is_not(None),
                PhotoAsset.quality_review_required.is_(True),
            )
            .order_by(
                PhotoAsset.quality_review_created_at.asc().nullsfirst(),
                PhotoAsset.updated_at.asc(),
            )
        ).all()
    )
    review_items = [
        {
            "photo": photo,
            "job": job,
            "step": step,
            "dealership": dealership,
            "original_url": storage.create_download_url(
                object_key=photo.original_thumbnail_object_key or photo.original_object_key
            ),
            "original_full_url": storage.create_download_url(
                object_key=photo.original_object_key
            ),
            "processed_url": (
                storage.create_download_url(
                    object_key=photo.processed_thumbnail_object_key
                    or photo.processed_object_key
                )
                if photo.processed_object_key
                and photo.processing_status == ProcessingStatus.COMPLETED
                else None
            ),
            "processed_full_url": (
                storage.create_download_url(object_key=photo.processed_object_key)
                if photo.processed_object_key
                and photo.processing_status == ProcessingStatus.COMPLETED
                else None
            ),
        }
        for photo, job, step, dealership in rows
    ]
    operators = (
        list(
            db.scalars(
                select(User)
                .where(User.role == UserRole.OPERATOR)
                .order_by(User.email)
            )
        )
        if admin.role == UserRole.SYSTEM_ADMIN
        else []
    )
    live_version = "|".join(
        f"{item['photo'].id}:{item['photo'].updated_at.isoformat()}"
        for item in review_items
    )
    return templates.TemplateResponse(
        request,
        "admin/quality_reviews.html",
        _context(
            request,
            admin,
            review_items=review_items,
            operators=operators,
            reviews_live_version=live_version,
            web_push_enabled=get_settings().web_push_enabled,
            web_push_public_key=get_settings().web_push_vapid_public_key or "",
        ),
    )


@router.post("/quality-reviews/{photo_id}/approve")
def approve_quality_review(
    photo_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_quality_operator(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    photo = db.get(PhotoAsset, photo_id)
    if photo is None or not photo.quality_review_required:
        raise HTTPException(status_code=404, detail="Prüffall wurde nicht gefunden")
    if (
        photo.processing_status != ProcessingStatus.COMPLETED
        or not photo.processed_object_key
    ):
        raise HTTPException(
            status_code=409,
            detail="Ein fehlgeschlagenes Bild muss zuerst korrigiert oder neu verarbeitet werden",
        )
    photo.quality_review_required = False
    photo.quality_reviewed_by_id = admin.id
    photo.quality_reviewed_at = datetime.now(timezone.utc)
    photo.quality_review_resolution = "approved"
    job = db.get(VehicleJob, photo.vehicle_job_id)
    db.commit()
    remaining_reviews = db.scalar(
        select(func.count(PhotoAsset.id)).where(
            PhotoAsset.vehicle_job_id == photo.vehicle_job_id,
            PhotoAsset.is_selected.is_(True),
            PhotoAsset.quality_review_required.is_(True),
        )
    )
    if job is not None and not remaining_reviews:
        try_enqueue_auto_export(job.id, db)
    _flash(request, "Bild wurde freigegeben und aus der Warteschlange entfernt.")
    return RedirectResponse("/admin/quality-reviews", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/quality-reviews/{photo_id}/reprocess")
def reprocess_quality_review(
    photo_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_quality_operator(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    photo = db.get(PhotoAsset, photo_id)
    if photo is None or not photo.quality_review_required:
        raise HTTPException(status_code=404, detail="Prüffall wurde nicht gefunden")
    job = db.get(VehicleJob, photo.vehicle_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    photo.processing_status = ProcessingStatus.QUEUED
    photo.processing_error = None
    photo.quality_reviewed_by_id = admin.id
    photo.quality_reviewed_at = datetime.now(timezone.utc)
    photo.quality_review_resolution = "reprocessing"
    job.status = JobStatus.PROCESSING
    db.commit()
    try:
        enqueue_photo_processing(photo.id)
    except ProcessingQueueUnavailable:
        photo.processing_status = ProcessingStatus.FAILED
        photo.processing_error = "Verarbeitungswarteschlange ist nicht erreichbar"
        job.status = JobStatus.REVIEW_REQUIRED
        db.commit()
        _flash(request, "Die erneute Verarbeitung konnte nicht gestartet werden.", "error")
    else:
        _flash(request, "Das Bild wird erneut automatisch verarbeitet.")
    return RedirectResponse("/admin/quality-reviews", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/operators")
def create_operator(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    normalized_email = _normalized_email(email)
    if normalized_email is None or len(password) < 16:
        _flash(request, "E-Mail oder Passwort (mindestens 16 Zeichen) ist ungültig.", "error")
    else:
        db.add(
            User(
                dealership_id=None,
                email=normalized_email,
                password_hash=hash_password(password),
                role=UserRole.OPERATOR,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _flash(request, "Diese E-Mail-Adresse ist bereits vorhanden.", "error")
        else:
            _flash(request, "Operator wurde angelegt.")
    return RedirectResponse("/admin/quality-reviews#operators", status_code=303)


@router.post("/operators/{operator_id}")
def update_operator(
    operator_id: uuid.UUID,
    request: Request,
    password: str = Form(default=""),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    operator = db.get(User, operator_id)
    if operator is None or operator.role != UserRole.OPERATOR:
        raise HTTPException(status_code=404, detail="Operator wurde nicht gefunden")
    if password and len(password) < 16:
        _flash(request, "Das neue Passwort muss mindestens 16 Zeichen lang sein.", "error")
    else:
        operator.is_active = is_active == "on"
        if password:
            operator.password_hash = hash_password(password)
        if password or not operator.is_active:
            db.execute(
                update(RefreshSession)
                .where(
                    RefreshSession.user_id == operator.id,
                    RefreshSession.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(timezone.utc))
            )
        db.commit()
        _flash(request, "Operator wurde gespeichert.")
    return RedirectResponse("/admin/quality-reviews#operators", status_code=303)


@router.get("/image-service", response_class=HTMLResponse)
def image_service_page(request: Request, db: Session = Depends(get_db)):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    image_settings = get_image_settings(db)
    runtime = get_settings()
    dealerships = list(db.scalars(select(Dealership).order_by(Dealership.name)))
    credit_grants = list(
        db.scalars(
            select(VehicleCreditGrant).order_by(VehicleCreditGrant.granted_at.desc()).limit(50)
        )
    )
    grant_user_ids = {grant.granted_by_id for grant in credit_grants}
    return templates.TemplateResponse(
        request,
        "admin/image_service.html",
        _context(
            request,
            admin,
            image_settings=image_settings,
            provider_available=provider_is_available(image_settings, runtime),
            remove_bg_key_configured=bool(runtime.remove_bg_api_key),
            photoroom_live_key_configured=bool(
                runtime.photoroom_key_for(sandbox=False)
            ),
            photoroom_sandbox_key_configured=bool(
                runtime.photoroom_key_for(sandbox=True)
            ),
            dealerships=dealerships,
            credit_balances={
                dealership.id: credit_balance(db, dealership) for dealership in dealerships
            },
            credit_grants=credit_grants,
            grant_dealerships={dealership.id: dealership for dealership in dealerships},
            grant_users={
                user.id: user
                for user in db.scalars(select(User).where(User.id.in_(grant_user_ids)))
            }
            if grant_user_ids
            else {},
        ),
    )


@router.post("/image-service")
def update_image_service(
    request: Request,
    provider: str = Form(),
    default_monthly_vehicle_credits: int = Form(),
    photoroom_sandbox: str | None = Form(default=None),
    comparison_mode_enabled: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    if admin.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    if (
        provider not in IMAGE_PROVIDERS
        or not 0 <= default_monthly_vehicle_credits <= 10000
    ):
        _flash(
            request,
            "Bitte prüfen Sie Bilddienstleister, Standardkontingent und Konturautomatik.",
            "error",
        )
    else:
        image_settings = get_image_settings(db)
        image_settings.provider = provider
        image_settings.photoroom_sandbox = photoroom_sandbox == "on"
        image_settings.comparison_mode_enabled = comparison_mode_enabled == "on"
        image_settings.default_monthly_vehicle_credits = default_monthly_vehicle_credits
        db.commit()
        if provider_is_available(image_settings, get_settings()) or provider == "disabled":
            _flash(request, "Bilddienstleister-Einstellungen wurden gespeichert.")
        else:
            _flash(
                request,
                "Einstellungen gespeichert, aber für den Anbieter fehlt das VPS-Secret.",
                "error",
            )
    return RedirectResponse("/admin/image-service", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/dealerships/{dealership_id}/credits")
def update_dealership_credits(
    dealership_id: uuid.UUID,
    request: Request,
    monthly_vehicle_credits: int = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    if admin.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    dealership = db.get(Dealership, dealership_id)
    if dealership is None:
        raise HTTPException(status_code=404, detail="Autohaus wurde nicht gefunden")
    if not 0 <= monthly_vehicle_credits <= 10000:
        _flash(request, "Das Monatskontingent muss zwischen 0 und 10.000 liegen.", "error")
    else:
        dealership.monthly_vehicle_credits = monthly_vehicle_credits
        db.commit()
        _flash(request, f"Credit-Kontingent für {dealership.name} wurde gespeichert.")
    return RedirectResponse("/admin/image-service", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/dealerships/{dealership_id}/credits/add")
def add_dealership_credits(
    dealership_id: uuid.UUID,
    request: Request,
    amount: int = Form(),
    note: str = Form(default=""),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    if admin.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    try:
        grant_additional_credits(db, dealership_id, admin, amount, note)
    except ValueError as exc:
        _flash(request, str(exc), "error")
    else:
        db.commit()
        _flash(request, f"{amount} zusätzliche Fahrzeug-Credits wurden gutgeschrieben.")
    return RedirectResponse(
        "/admin/image-service#dealership-credits",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dealerships")
def create_dealership(
    request: Request,
    name: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    if admin.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    cleaned_name = name.strip()
    if not cleaned_name:
        _flash(request, "Bitte geben Sie einen Namen ein.", "error")
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    if _dealership_name_exists(db, cleaned_name):
        _flash(request, "Ein Autohaus mit diesem Namen ist bereits vorhanden.", "error")
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    image_settings = get_image_settings(db)
    dealership = Dealership(
        name=cleaned_name,
        retention_days=90,
        auto_export_enabled=False,
        monthly_vehicle_credits=image_settings.default_monthly_vehicle_credits,
    )
    db.add(dealership)
    db.commit()
    db.refresh(dealership)
    _flash(request, "Autohaus wurde angelegt.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/dealerships/{dealership_id}", response_class=HTMLResponse)
def dealership_detail(
    dealership_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    dealership = _authorized_dealership(db, admin, dealership_id)
    locations = list(
        db.scalars(
            select(Location).where(Location.dealership_id == dealership.id).order_by(Location.name)
        )
    )
    users = list(
        db.scalars(select(User).where(User.dealership_id == dealership.id).order_by(User.email))
    )
    balance = credit_balance(db, dealership)
    sftp_settings = db.get(DealershipSftpSettings, dealership.id)
    return templates.TemplateResponse(
        request,
        "admin/dealership_detail.html",
        _context(
            request,
            admin,
            dealership=dealership,
            locations=locations,
            users=users,
            user_roles=UserRole,
            credit_balance=balance,
            sftp_settings=sftp_settings,
            sftp_password_configured=bool(sftp_settings and sftp_settings.password_encrypted),
        ),
    )


@router.get("/orientations", response_class=HTMLResponse)
def orientations_page(
    request: Request,
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    orientations = _ensure_standard_orientations(db)
    category_labels = {
        "exterior": "Außenaufnahmen",
        "interior": "Innenraum",
        "detail": "Details",
        "special": "Spezialaufnahmen",
    }
    orientation_groups = [
        (
            category,
            category_labels[category],
            [item for item in orientations if item.category == category],
        )
        for category in ("exterior", "interior", "detail", "special")
    ]
    silhouette_previews = {}
    for orientation in orientations:
        if orientation.silhouette_object_key:
            silhouette_previews[orientation.id] = storage.create_download_url(
                object_key=orientation.silhouette_object_key
            )
            continue
        default_path = default_silhouette_path(orientation.key)
        if default_path:
            silhouette_previews[orientation.id] = str(
                request.url_for("admin-static", path=default_path)
            )
    return templates.TemplateResponse(
        request,
        "admin/orientations.html",
        _context(
            request,
            admin,
            orientations=orientations,
            orientation_groups=orientation_groups,
            silhouette_previews=silhouette_previews,
        ),
    )


@router.post("/orientations")
async def create_orientation(
    request: Request,
    key: str = Form(),
    name: str = Form(),
    instruction: str = Form(default=""),
    category: str = Form(default="detail"),
    default_capture_order: int = Form(),
    default_export_order: int | None = Form(default=None),
    is_required: str | None = Form(default=None),
    processing_mode: str = Form(default="original"),
    is_repeatable: str | None = Form(default=None),
    default_instance_count: int = Form(default=1),
    max_instances: int = Form(default=1),
    silhouette: UploadFile | None = File(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    if (
        not key.strip()
        or not name.strip()
        or category not in ORIENTATION_CATEGORIES
        or processing_mode not in PROCESSING_MODES
        or default_capture_order < 1
        or (default_export_order is not None and default_export_order < 1)
        or default_instance_count < 1
        or max_instances < default_instance_count
        or max_instances > 50
        or (is_repeatable != "on" and default_instance_count != 1)
    ):
        _flash(
            request, "Bitte prüfen Sie Schlüssel, Name, Verarbeitung und Wiederholungen.", "error"
        )
    else:
        orientation = Orientation(
            key=key.strip().lower(),
            name=name.strip(),
            instruction=instruction.strip(),
            category=category,
            default_capture_order=default_capture_order,
            default_export_order=default_export_order,
            is_required=is_required == "on",
            requires_processing=processing_mode in PROCESSING_REQUIRED_MODES,
            processing_mode=processing_mode,
            is_repeatable=is_repeatable == "on",
            default_instance_count=default_instance_count,
            max_instances=max_instances if is_repeatable == "on" else 1,
        )
        if silhouette is not None and silhouette.filename:
            object_key, content_type = await _store_configuration_image(
                storage,
                silhouette,
                object_key_prefix="configuration/orientations/silhouettes",
                png_only=True,
            )
            orientation.silhouette_object_key = object_key
            orientation.silhouette_content_type = content_type
        db.add(orientation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _flash(request, "Schlüssel oder Name ist bereits vorhanden.", "error")
        else:
            _flash(request, "Orientierung wurde angelegt.")
    return RedirectResponse("/admin/orientations", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/orientations/{orientation_id}")
async def update_orientation(
    orientation_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    instruction: str = Form(default=""),
    category: str = Form(default="detail"),
    default_capture_order: int = Form(),
    default_export_order: int | None = Form(default=None),
    is_required: str | None = Form(default=None),
    processing_mode: str = Form(default="original"),
    is_repeatable: str | None = Form(default=None),
    default_instance_count: int = Form(default=1),
    max_instances: int = Form(default=1),
    is_active: str | None = Form(default=None),
    silhouette: UploadFile | None = File(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    orientation = db.get(Orientation, orientation_id)
    if orientation is None:
        raise HTTPException(status_code=404, detail="Orientierung wurde nicht gefunden")
    if (
        not name.strip()
        or category not in ORIENTATION_CATEGORIES
        or processing_mode not in PROCESSING_MODES
        or default_capture_order < 1
        or (default_export_order is not None and default_export_order < 1)
        or default_instance_count < 1
        or max_instances < default_instance_count
        or max_instances > 50
        or (is_repeatable != "on" and default_instance_count != 1)
    ):
        _flash(request, "Bitte prüfen Sie Name, Verarbeitung und Wiederholungen.", "error")
    else:
        orientation.name = name.strip()
        orientation.instruction = instruction.strip()
        orientation.category = category
        orientation.default_capture_order = default_capture_order
        orientation.default_export_order = default_export_order
        orientation.is_required = is_required == "on"
        orientation.processing_mode = processing_mode
        orientation.requires_processing = processing_mode in PROCESSING_REQUIRED_MODES
        orientation.is_repeatable = is_repeatable == "on"
        orientation.default_instance_count = default_instance_count
        orientation.max_instances = max_instances if orientation.is_repeatable else 1
        orientation.is_active = is_active == "on"
        if silhouette is not None and silhouette.filename:
            object_key, content_type = await _store_configuration_image(
                storage,
                silhouette,
                object_key_prefix="configuration/orientations/silhouettes",
                png_only=True,
            )
            orientation.silhouette_object_key = object_key
            orientation.silhouette_content_type = content_type
        for step in db.scalars(
            select(CaptureStep).where(CaptureStep.orientation_id == orientation.id)
        ):
            step.name = instance_name(
                orientation.name,
                step.orientation_instance_index,
                orientation.is_repeatable,
            )
            step.instruction = orientation.instruction
            step.category = orientation.category
            if processing_mode != "configurable":
                step.requires_processing = orientation.requires_processing
            if orientation.silhouette_object_key:
                step.silhouette_object_key = orientation.silhouette_object_key
                step.silhouette_content_type = orientation.silhouette_content_type
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _flash(request, "Der Name ist bereits vorhanden.", "error")
        else:
            _flash(request, "Orientierung wurde gespeichert.")
    return RedirectResponse("/admin/orientations", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/dealerships/{dealership_id}/sftp")
def update_dealership_sftp(
    dealership_id: uuid.UUID,
    request: Request,
    host: str = Form(default=""),
    port: int = Form(default=22),
    username: str = Form(default=""),
    password: str = Form(default=""),
    remote_directory: str = Form(default="/"),
    host_key_fingerprint: str = Form(default=""),
    is_enabled: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    config = db.get(DealershipSftpSettings, dealership.id)
    if config is None:
        config = DealershipSftpSettings(dealership_id=dealership.id)
        db.add(config)
    try:
        config.host = host.strip()
        config.port = port
        config.username = username.strip()
        config.remote_directory = normalize_remote_directory(remote_directory)
        config.host_key_fingerprint = (
            normalize_fingerprint(host_key_fingerprint) if host_key_fingerprint.strip() else ""
        )
        if password:
            config.password_encrypted = encrypt_password(password, get_settings())
        config.is_enabled = is_enabled == "on"
        if config.is_enabled:
            validate_sftp_settings(config, get_settings())
    except SftpConfigurationError as exc:
        db.rollback()
        _flash(request, str(exc), "error")
    else:
        config.last_test_successful = None
        config.last_test_error = None
        db.commit()
        _flash(request, "SFTP-Einstellungen wurden gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}#sftp", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/dealerships/{dealership_id}/sftp/test")
def test_dealership_sftp(
    dealership_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    config = db.get(DealershipSftpSettings, dealership.id)
    if config is None:
        _flash(request, "Bitte speichern Sie zuerst die SFTP-Einstellungen.", "error")
    else:
        try:
            test_sftp_connection(config, get_settings())
        except Exception as exc:
            config.last_tested_at = datetime.now(timezone.utc)
            config.last_test_successful = False
            config.last_test_error = str(exc)[:1000]
            db.commit()
            _flash(request, f"SFTP-Verbindung fehlgeschlagen: {exc}", "error")
        else:
            config.last_tested_at = datetime.now(timezone.utc)
            config.last_test_successful = True
            config.last_test_error = None
            db.commit()
            _flash(request, "SFTP-Verbindung erfolgreich geprüft.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}#sftp", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/dealerships/{dealership_id}/sftp/fingerprint")
def fetch_dealership_sftp_fingerprint(
    dealership_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    config = db.get(DealershipSftpSettings, dealership.id)
    if config is None or not config.host.strip():
        _flash(request, "Bitte speichern Sie zuerst SFTP-Server und Port.", "error")
    else:
        try:
            fingerprint = fetch_host_key_fingerprint(config.host, config.port)
        except Exception as exc:
            _flash(request, str(exc), "error")
        else:
            config.host_key_fingerprint = fingerprint
            config.last_test_successful = None
            config.last_test_error = None
            db.commit()
            _flash(request, f"Hostschlüssel wurde abgerufen: {fingerprint}")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}#sftp", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/dealerships/{dealership_id}")
def update_dealership(
    dealership_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    retention_days: int = Form(),
    auto_export_enabled: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    cleaned_name = name.strip()
    if not cleaned_name or retention_days < 1 or retention_days > 365:
        _flash(request, "Bitte prüfen Sie Name und Aufbewahrungsdauer.", "error")
    elif _dealership_name_exists(db, cleaned_name, excluding_id=dealership.id):
        _flash(request, "Ein Autohaus mit diesem Namen ist bereits vorhanden.", "error")
    else:
        dealership.name = cleaned_name
        dealership.retention_days = retention_days
        dealership.auto_export_enabled = auto_export_enabled == "on"
        dealership.is_active = is_active == "on"
        db.commit()
        _flash(request, "Autohaus-Einstellungen wurden gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/dealerships/{dealership_id}/locations")
def create_location(
    dealership_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    cleaned_name = name.strip()
    if cleaned_name and not _location_name_exists(db, dealership.id, cleaned_name):
        db.add(Location(dealership_id=dealership.id, name=cleaned_name))
        db.commit()
        _flash(request, "Standort wurde angelegt.")
    elif cleaned_name:
        _flash(request, "Dieser Standort ist bereits vorhanden.", "error")
    else:
        _flash(request, "Bitte geben Sie einen Standortnamen ein.", "error")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}#locations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/locations/{location_id}")
def update_location(
    location_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="Standort wurde nicht gefunden")
    _authorized_dealership(db, admin, location.dealership_id)
    cleaned_name = name.strip()
    if cleaned_name and not _location_name_exists(
        db,
        location.dealership_id,
        cleaned_name,
        excluding_id=location.id,
    ):
        location.name = cleaned_name
        location.is_active = is_active == "on"
        db.commit()
        _flash(request, "Standort wurde gespeichert.")
    elif cleaned_name:
        _flash(request, "Dieser Standort ist bereits vorhanden.", "error")
    else:
        _flash(request, "Bitte geben Sie einen Standortnamen ein.", "error")
    return RedirectResponse(
        f"/admin/dealerships/{location.dealership_id}#locations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dealerships/{dealership_id}/users")
def create_user(
    dealership_id: uuid.UUID,
    request: Request,
    email: str = Form(),
    password: str = Form(),
    role: UserRole = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    normalized_email = _normalized_email(email)
    if normalized_email is None:
        _flash(request, "Bitte geben Sie eine gültige E-Mail-Adresse ein.", "error")
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}#users",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if role in {UserRole.SYSTEM_ADMIN, UserRole.OPERATOR} or len(password) < 16:
        _flash(request, "Rolle oder Passwort ist nicht zulässig.", "error")
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}#users",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    user = User(
        dealership_id=dealership.id,
        email=normalized_email,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _flash(request, "Diese E-Mail-Adresse ist bereits vorhanden.", "error")
    else:
        _flash(request, "Benutzer wurde angelegt.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}#users",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/users/{user_id}")
def update_user(
    user_id: uuid.UUID,
    request: Request,
    password: str = Form(default=""),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    user = db.get(User, user_id)
    if user is None or user.dealership_id is None:
        raise HTTPException(status_code=404, detail="Benutzer wurde nicht gefunden")
    _authorized_dealership(db, admin, user.dealership_id)
    if user.id == admin.id and is_active != "on":
        _flash(request, "Das eigene Konto kann nicht deaktiviert werden.", "error")
    elif password and len(password) < 16:
        _flash(request, "Das neue Passwort muss mindestens 16 Zeichen lang sein.", "error")
    else:
        user.is_active = is_active == "on"
        if password:
            user.password_hash = hash_password(password)
        if password or not user.is_active:
            db.execute(
                update(RefreshSession)
                .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
                .values(revoked_at=datetime.now(timezone.utc))
            )
        db.commit()
        _flash(request, "Benutzer wurde gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{user.dealership_id}#users",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/dealerships/{dealership_id}/jobs", response_class=HTMLResponse)
def jobs_page(
    dealership_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    dealership = _authorized_dealership(db, admin, dealership_id)
    jobs = list(
        db.scalars(
            select(VehicleJob)
            .where(VehicleJob.dealership_id == dealership.id)
            .order_by(VehicleJob.created_at.desc())
        )
    )
    locations = {
        location.id: location
        for location in db.scalars(
            select(Location).where(
                Location.dealership_id == dealership.id,
                Location.is_active.is_(True),
            )
        )
    }
    brands = list(
        db.scalars(
            select(Brand)
            .where(Brand.dealership_id == dealership.id, Brand.is_active.is_(True))
            .order_by(Brand.name)
        )
    )
    backgrounds = list(
        db.scalars(
            select(Background)
            .options(selectinload(Background.locations))
            .where(Background.dealership_id == dealership.id, Background.is_active.is_(True))
            .order_by(Background.name)
        )
    )
    balance = credit_balance(db, dealership)
    jobs_live_version = "|".join(
        f"{job.id}:{job.updated_at.isoformat()}:{job.status.value}" for job in jobs
    )
    return templates.TemplateResponse(
        request,
        "admin/jobs.html",
        _context(
            request,
            admin,
            dealership=dealership,
            jobs=jobs,
            locations=locations,
            brands=brands,
            backgrounds=backgrounds,
            credit_balance=balance,
            jobs_live_version=jobs_live_version,
        ),
    )


@router.post("/dealerships/{dealership_id}/jobs")
def create_job_from_admin(
    dealership_id: uuid.UUID,
    request: Request,
    vin: str = Form(),
    location_id: str = Form(),
    brand_id: str = Form(),
    background_id: str = Form(default=""),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    try:
        selected_location_id = uuid.UUID(location_id)
        selected_brand_id = uuid.UUID(brand_id)
        selected_background_id = _optional_uuid(background_id)
    except (ValueError, HTTPException):
        _flash(request, "Bitte Standort, Marke und Hintergrund korrekt auswählen.", "error")
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}/jobs",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    location = db.get(Location, selected_location_id)
    brand = db.get(Brand, selected_brand_id)
    background = db.get(Background, selected_background_id) if selected_background_id else None
    normalized_vin = vin.strip().upper()
    error: str | None = None
    if not normalized_vin or len(normalized_vin) > 64:
        error = "Bitte eine Fahrgestellnummer mit höchstens 64 Zeichen eingeben."
    elif location is None or not location.is_active or location.dealership_id != dealership.id:
        error = "Der gewählte Standort ist nicht verfügbar."
    elif brand is None or not brand.is_active or brand.dealership_id != dealership.id:
        error = "Die gewählte Marke ist nicht verfügbar."
    elif background is not None and (
        not background.is_active
        or background.dealership_id != dealership.id
        or (background.brand_id is not None and background.brand_id != brand.id)
        or (background.locations and location not in background.locations)
    ):
        error = "Der gewählte Hintergrund passt nicht zu Standort und Marke."
    if error:
        _flash(request, error, "error")
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}/jobs",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    db.scalar(select(Dealership).where(Dealership.id == dealership.id).with_for_update())
    latest_version = db.scalar(
        select(func.max(VehicleJob.version)).where(
            VehicleJob.dealership_id == dealership.id,
            VehicleJob.vin == normalized_vin,
        )
    )
    job = VehicleJob(
        dealership_id=dealership.id,
        location_id=location.id,
        created_by_id=admin.id,
        vin=normalized_vin,
        version=(latest_version or 0) + 1,
        brand=brand.name,
        brand_id=brand.id,
        background_id=background.id if background else None,
        status=JobStatus.DRAFT,
        auto_export=False,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _flash(
            request,
            "Der Auftrag konnte wegen einer parallelen Anlage nicht erstellt werden.",
            "error",
        )
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}/jobs",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    _flash(request, f"Auftrag {job.vin} · Version {job.version} wurde angelegt.")
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/photos")
async def upload_job_photo_from_admin(
    job_id: uuid.UUID,
    request: Request,
    capture_step_id: str = Form(),
    original_image: UploadFile = File(),
    benchmark_image: UploadFile | None = File(default=None),
    start_processing: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    job = db.get(VehicleJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    _authorized_dealership(db, admin, job.dealership_id)
    try:
        selected_step_id = uuid.UUID(capture_step_id)
    except ValueError:
        _flash(request, "Bitte eine Fotoposition auswählen.", "error")
        return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)
    step = db.get(CaptureStep, selected_step_id)
    if step is None or not step.is_active or step.dealership_id != job.dealership_id:
        _flash(request, "Die gewählte Fotoposition ist nicht verfügbar.", "error")
        return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    try:
        original, original_type, original_extension = await _read_job_photo(original_image)
        original_thumbnail = create_thumbnail(original)
        benchmark: bytes | None = None
        benchmark_type: str | None = None
        benchmark_extension: str | None = None
        benchmark_thumbnail: bytes | None = None
        if benchmark_image is not None and benchmark_image.filename:
            benchmark, benchmark_type, benchmark_extension = await _read_job_photo(benchmark_image)
            benchmark_thumbnail = create_thumbnail(benchmark)
    except (ValueError, ThumbnailError) as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    latest_revision = db.scalar(
        select(func.max(PhotoAsset.revision)).where(
            PhotoAsset.vehicle_job_id == job.id,
            PhotoAsset.capture_step_id == step.id,
        )
    )
    photo_id = uuid.uuid4()
    original_key = (
        f"dealerships/{job.dealership_id}/jobs/{job.id}/originals/"
        f"{step.id}/{photo_id}.{original_extension}"
    )
    original_thumbnail_key = thumbnail_key(original_key)
    benchmark_key = (
        f"dealerships/{job.dealership_id}/jobs/{job.id}/benchmarks/"
        f"{step.id}/{photo_id}.{benchmark_extension}"
        if benchmark is not None
        else None
    )
    benchmark_thumbnail_key = thumbnail_key(benchmark_key) if benchmark_key else None
    try:
        storage.put_object(
            object_key=original_key,
            content=original,
            content_type=original_type,
        )
        storage.put_object(
            object_key=original_thumbnail_key,
            content=original_thumbnail,
            content_type="image/jpeg",
        )
        if benchmark is not None and benchmark_key and benchmark_thumbnail_key:
            storage.put_object(
                object_key=benchmark_key,
                content=benchmark,
                content_type=benchmark_type or "image/jpeg",
            )
            storage.put_object(
                object_key=benchmark_thumbnail_key,
                content=benchmark_thumbnail or b"",
                content_type="image/jpeg",
            )
    except StorageUnavailableError:
        _flash(request, "Der Bildspeicher ist nicht erreichbar.", "error")
        return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    db.execute(
        update(PhotoAsset)
        .where(
            PhotoAsset.vehicle_job_id == job.id,
            PhotoAsset.capture_step_id == step.id,
        )
        .values(is_selected=False)
    )
    photo = PhotoAsset(
        id=photo_id,
        vehicle_job_id=job.id,
        capture_step_id=step.id,
        captured_by_id=admin.id,
        revision=(latest_revision or 0) + 1,
        original_object_key=original_key,
        original_content_type=original_type,
        expected_size_bytes=len(original),
        original_size_bytes=len(original),
        original_thumbnail_object_key=original_thumbnail_key,
        benchmark_object_key=benchmark_key,
        benchmark_thumbnail_object_key=benchmark_thumbnail_key,
        benchmark_content_type=benchmark_type,
        benchmark_size_bytes=len(benchmark) if benchmark is not None else None,
        uploaded_at=datetime.now(timezone.utc),
        is_selected=True,
        processing_status=(
            ProcessingStatus.PENDING if step.requires_processing else ProcessingStatus.NOT_REQUIRED
        ),
    )
    db.add(photo)
    job.status = JobStatus.REVIEW_REQUIRED
    db.commit()

    process_now = start_processing == "on" and step.requires_processing
    image_settings = get_image_settings(db)
    runtime = get_settings()
    if process_now and job.background_id is None:
        _flash(
            request,
            "Foto gespeichert. Ohne gewählten Hintergrund kann die Optimierung nicht starten.",
            "error",
        )
    elif process_now and provider_is_available(image_settings, runtime):
        try:
            reserve_vehicle_credit(db, job, image_settings.provider)
        except VehicleCreditsExhausted as exc:
            photo.processing_error = str(exc)
            db.commit()
            _flash(request, f"Foto gespeichert. {exc}", "error")
        else:
            photo.processing_status = ProcessingStatus.QUEUED
            photo.processing_error = None
            job.status = JobStatus.PROCESSING
            db.commit()
            try:
                enqueue_photo_processing(photo.id)
            except ProcessingQueueUnavailable:
                photo.processing_status = ProcessingStatus.FAILED
                photo.processing_error = "Verarbeitungswarteschlange ist nicht erreichbar"
                job.status = JobStatus.REVIEW_REQUIRED
                db.commit()
                _flash(
                    request,
                    "Foto gespeichert, aber die Verarbeitung konnte nicht starten.",
                    "error",
                )
            else:
                _flash(request, "Foto gespeichert und zur Optimierung vorgemerkt.")
    elif process_now:
        _flash(request, "Foto gespeichert. Der Bilddienst ist noch nicht verfügbar.", "error")
    else:
        _flash(
            request,
            "Foto und Referenz wurden gespeichert." if benchmark else "Foto wurde gespeichert.",
        )
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail_page(
    job_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    job = db.get(VehicleJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, job.dealership_id)
    photos = list(
        db.scalars(
            select(PhotoAsset)
            .where(
                PhotoAsset.vehicle_job_id == job.id,
                PhotoAsset.is_selected.is_(True),
                PhotoAsset.uploaded_at.is_not(None),
            )
            .order_by(PhotoAsset.created_at)
        )
    )
    steps = {
        step.id: step
        for step in db.scalars(
            select(CaptureStep).where(CaptureStep.dealership_id == dealership.id)
        )
    }
    active_steps = sorted(
        (step for step in steps.values() if step.is_active),
        key=lambda step: (step.capture_order, step.name),
    )
    window_orientation_ids = set(
        db.scalars(
            select(Orientation.id).where(
                Orientation.processing_mode.in_(MASKED_BACKGROUND_MODES)
            )
        )
    )
    window_background_step_ids = {
        step.id for step in steps.values() if step.orientation_id in window_orientation_ids
    }
    variants = (
        list(
            db.scalars(
                select(PhotoProcessingVariant).where(
                    PhotoProcessingVariant.photo_asset_id.in_([photo.id for photo in photos])
                )
            )
        )
        if photos
        else []
    )
    photoroom_variants = {
        variant.photo_asset_id: variant for variant in variants if variant.provider == "photoroom"
    }
    optimized_photoroom_variants = {
        variant.photo_asset_id: variant
        for variant in variants
        if variant.provider == "photoroom_optimized"
    }
    runtime = get_settings()
    image_settings = get_image_settings(db)
    export_runs = list(
        db.scalars(
            select(ExportRun)
            .where(ExportRun.vehicle_job_id == job.id)
            .order_by(ExportRun.attempt.desc(), ExportRun.created_at.desc())
        )
    )
    job_live_version = f"{job.updated_at.isoformat()}:{job.status.value}"
    photos_live_version = "|".join(
        [
            *(
                f"{photo.id}:{photo.updated_at.isoformat()}:{photo.processing_status.value}:"
                f"{photo.processed_object_key or ''}:{photo.processed_thumbnail_object_key or ''}"
                for photo in photos
            ),
            *(
                f"{variant.id}:{variant.updated_at.isoformat()}:{variant.status}:"
                f"{variant.object_key or ''}:{variant.thumbnail_object_key or ''}"
                for variant in variants
            ),
        ]
    )
    exports_live_version = "|".join(
        f"{export_run.id}:{export_run.updated_at.isoformat()}:{export_run.status}:"
        f"{export_run.transfer_status}:{export_run.object_key or ''}"
        for export_run in export_runs
    )
    return templates.TemplateResponse(
        request,
        "admin/job_detail.html",
        _context(
            request,
            admin,
            dealership=dealership,
            job=job,
            photos=photos,
            steps=steps,
            active_steps=active_steps,
            window_background_step_ids=window_background_step_ids,
            original_urls={
                photo.id: storage.create_download_url(object_key=photo.original_object_key)
                for photo in photos
            },
            original_preview_urls={
                photo.id: storage.create_download_url(
                    object_key=photo.original_thumbnail_object_key or photo.original_object_key
                )
                for photo in photos
            },
            original_download_urls={
                photo.id: storage.create_download_url(
                    object_key=photo.original_object_key,
                    filename=f"{job.vin}_{index:02d}_Original.jpg",
                )
                for index, photo in enumerate(photos, start=1)
            },
            benchmark_urls={
                photo.id: storage.create_download_url(object_key=photo.benchmark_object_key)
                for photo in photos
                if photo.benchmark_object_key
            },
            benchmark_preview_urls={
                photo.id: storage.create_download_url(
                    object_key=photo.benchmark_thumbnail_object_key or photo.benchmark_object_key
                )
                for photo in photos
                if photo.benchmark_object_key
            },
            benchmark_download_urls={
                photo.id: storage.create_download_url(
                    object_key=photo.benchmark_object_key,
                    filename=f"{job.vin}_{index:02d}_Referenz.jpg",
                )
                for index, photo in enumerate(photos, start=1)
                if photo.benchmark_object_key
            },
            processed_urls={
                photo.id: storage.create_download_url(object_key=photo.processed_object_key)
                for photo in photos
                if photo.processed_object_key
            },
            processed_preview_urls={
                photo.id: storage.create_download_url(
                    object_key=photo.processed_thumbnail_object_key or photo.processed_object_key
                )
                for photo in photos
                if photo.processed_object_key
            },
            processed_download_urls={
                photo.id: storage.create_download_url(
                    object_key=photo.processed_object_key,
                    filename=f"{job.vin}_{index:02d}_Optimiert.jpg",
                )
                for index, photo in enumerate(photos, start=1)
                if photo.processed_object_key
            },
            photoroom_variants=photoroom_variants,
            optimized_photoroom_variants=optimized_photoroom_variants,
            photoroom_urls={
                variant.photo_asset_id: storage.create_download_url(object_key=variant.object_key)
                for variant in variants
                if variant.provider == "photoroom" and variant.object_key
            },
            photoroom_preview_urls={
                variant.photo_asset_id: storage.create_download_url(
                    object_key=variant.thumbnail_object_key or variant.object_key
                )
                for variant in variants
                if variant.provider == "photoroom" and variant.object_key
            },
            optimized_photoroom_urls={
                variant.photo_asset_id: storage.create_download_url(object_key=variant.object_key)
                for variant in variants
                if variant.provider == "photoroom_optimized" and variant.object_key
            },
            optimized_photoroom_preview_urls={
                variant.photo_asset_id: storage.create_download_url(
                    object_key=variant.thumbnail_object_key or variant.object_key
                )
                for variant in variants
                if variant.provider == "photoroom_optimized" and variant.object_key
            },
            optimized_photoroom_download_urls={
                variant.photo_asset_id: storage.create_download_url(
                    object_key=variant.object_key,
                    filename=f"{job.vin}_{index:02d}_Optimiert_Vergleich.jpg",
                )
                for index, photo in enumerate(photos, start=1)
                for variant in variants
                if variant.photo_asset_id == photo.id
                and variant.provider == "photoroom_optimized"
                and variant.object_key
            },
            processing_enabled=provider_is_available(image_settings, runtime),
            comparison_mode_enabled=image_settings.comparison_mode_enabled,
            standard_comparison_enabled=image_settings.provider != "photoroom",
            photoroom_enabled=runtime.photoroom_enabled,
            photoroom_sandbox=photoroom_sandbox_active(image_settings, runtime),
            credit_balance=credit_balance(db, dealership),
            job_live_version=job_live_version,
            photos_live_version=photos_live_version,
            exports_live_version=exports_live_version,
            export_runs=export_runs,
            export_download_urls={
                export_run.id: storage.create_download_url(
                    object_key=export_run.object_key,
                    filename=export_run.zip_filename,
                    expires_in=3600,
                )
                for export_run in export_runs
                if export_run.object_key and export_run.status == "completed"
            },
        ),
    )


@router.post("/photos/{photo_id}/process")
def reprocess_photo(
    photo_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    provider: str = Form(default="primary"),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    photo = db.get(PhotoAsset, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Foto wurde nicht gefunden")
    job = db.get(VehicleJob, photo.vehicle_job_id)
    step = db.get(CaptureStep, photo.capture_step_id)
    if job is None or step is None:
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    _authorized_dealership(db, admin, job.dealership_id)
    runtime = get_settings()
    image_settings = get_image_settings(db)
    comparison_provider = {
        "photoroom_comparison": "photoroom",
        "photoroom_optimized_comparison": "photoroom_optimized",
    }.get(provider)
    if not step.requires_processing:
        _flash(request, "Diese Fotoposition benötigt keine Freistellung.", "error")
    elif comparison_provider and not image_settings.comparison_mode_enabled:
        _flash(request, "Der Vergleichsmodus ist deaktiviert.", "error")
    elif comparison_provider and not runtime.photoroom_enabled:
        _flash(request, "Der Vergleichsdienst ist noch nicht konfiguriert.", "error")
    elif comparison_provider:
        variant = db.scalar(
            select(PhotoProcessingVariant).where(
                PhotoProcessingVariant.photo_asset_id == photo.id,
                PhotoProcessingVariant.provider == comparison_provider,
            )
        )
        if variant is None:
            variant = PhotoProcessingVariant(
                photo_asset_id=photo.id,
                provider=comparison_provider,
            )
            db.add(variant)
        variant.status = ProcessingStatus.QUEUED.value
        variant.error = None
        db.commit()
        try:
            enqueue_photo_variant(photo.id, comparison_provider)
        except ProcessingQueueUnavailable:
            variant.status = ProcessingStatus.FAILED.value
            variant.error = "Verarbeitungswarteschlange ist nicht erreichbar"
            db.commit()
            _flash(request, "Der Vergleich konnte nicht gestartet werden.", "error")
        else:
            label = "Optimierter" if comparison_provider.endswith("optimized") else "Standard"
            _flash(request, f"{label}-Vergleich wurde zur Verarbeitung vorgemerkt.")
    elif provider != "primary":
        _flash(request, "Unbekannter Bildverarbeitungsdienst.", "error")
    elif not provider_is_available(image_settings, runtime):
        _flash(request, "Es ist noch kein KI-Dienst konfiguriert.", "error")
    else:
        try:
            reserve_vehicle_credit(db, job, image_settings.provider)
        except VehicleCreditsExhausted as exc:
            photo.processing_status = ProcessingStatus.PENDING
            photo.processing_error = str(exc)
            db.commit()
            _flash(request, str(exc), "error")
        else:
            photo.processing_status = ProcessingStatus.QUEUED
            photo.processing_error = None
            job.status = JobStatus.PROCESSING
            db.commit()
            try:
                enqueue_photo_processing(photo.id)
            except ProcessingQueueUnavailable:
                photo.processing_status = ProcessingStatus.FAILED
                photo.processing_error = "Verarbeitungswarteschlange ist nicht erreichbar"
                job.status = JobStatus.REVIEW_REQUIRED
                db.commit()
                _flash(request, "Die Verarbeitung konnte nicht gestartet werden.", "error")
            else:
                _flash(request, "Das Foto wurde zur Verarbeitung vorgemerkt.")
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/photos/{photo_id}/request-improvement")
def request_photo_improvement(
    photo_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    if admin.role != UserRole.DEALERSHIP_ADMIN:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    photo = db.get(PhotoAsset, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Foto wurde nicht gefunden")
    job = db.get(VehicleJob, photo.vehicle_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    _authorized_dealership(db, admin, job.dealership_id)
    if not photo.processed_object_key:
        raise HTTPException(
            status_code=409,
            detail="Es liegt noch kein optimiertes Ergebnis zur Prüfung vor",
        )
    if photo.quality_review_required:
        _flash(request, "Das Bild wurde bereits zur Verbesserung vorgelegt.")
    else:
        photo.quality_review_required = True
        photo.quality_review_reason = (
            "Das Autohaus hat das Ergebnis zur Verbesserung vorgelegt."
        )
        photo.quality_review_created_at = datetime.now(timezone.utc)
        photo.quality_reviewed_by_id = None
        photo.quality_reviewed_at = None
        photo.quality_review_resolution = "requested_by_dealership"
        job.status = JobStatus.REVIEW_REQUIRED
        db.commit()
        _flash(request, "Das Bild wurde dem Operator zur Verbesserung vorgelegt.")
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


def _window_correction_photo(
    db: Session, admin: User, photo_id: uuid.UUID
) -> tuple[PhotoAsset, VehicleJob, CaptureStep, Orientation]:
    photo = db.get(PhotoAsset, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Foto wurde nicht gefunden")
    job = db.get(VehicleJob, photo.vehicle_job_id)
    step = db.get(CaptureStep, photo.capture_step_id)
    orientation = db.get(Orientation, step.orientation_id) if step else None
    if job is None or step is None or orientation is None:
        raise HTTPException(status_code=404, detail="Fotoposition wurde nicht gefunden")
    if admin.role not in {UserRole.SYSTEM_ADMIN, UserRole.OPERATOR}:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    if orientation.processing_mode not in MASKED_BACKGROUND_MODES:
        raise HTTPException(
            status_code=400,
            detail="Diese Fotoposition verwendet keine maskierte Hintergrundfläche",
        )
    return photo, job, step, orientation


@router.get("/photos/{photo_id}/correction", response_class=HTMLResponse)
def window_correction(
    photo_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = _require_quality_operator(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    photo, job, step, orientation = _window_correction_photo(db, admin, photo_id)
    background = db.get(Background, job.background_id) if job.background_id else None
    if not photo.window_mask_object_key:
        _flash(
            request,
            "Bitte starten Sie zuerst die Verarbeitung, damit eine Hintergrundmaske erzeugt wird.",
            "error",
        )
        return RedirectResponse(
            f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )
    override = (
        db.scalar(
            select(BackgroundOrientationComposition).where(
                BackgroundOrientationComposition.background_id == background.id,
                BackgroundOrientationComposition.orientation_id == orientation.id,
            )
        )
        if background is not None
        else None
    )
    inherited_shift = (
        override.window_background_shift_percent
        if override is not None
        and override.window_background_shift_percent is not None
        else (background.window_background_shift_percent if background else 14)
    )
    shift = (
        photo.window_background_shift_percent
        if photo.window_background_shift_percent is not None
        else inherited_shift
    )
    return templates.TemplateResponse(
        request,
        "admin/window_correction.html",
        _context(
            request,
            admin,
            photo=photo,
            job=job,
            step=step,
            background_shift_percent=shift,
        ),
    )


@router.get("/photos/{photo_id}/correction/{asset_kind}")
def window_correction_asset(
    photo_id: uuid.UUID,
    asset_kind: str,
    request: Request,
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_quality_operator(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    photo, job, _, _ = _window_correction_photo(db, admin, photo_id)
    if asset_kind == "original":
        object_key = photo.original_object_key
        content_type = photo.original_content_type
    elif asset_kind == "mask" and photo.window_mask_object_key:
        object_key = photo.window_mask_object_key
        content_type = "image/png"
    elif asset_kind == "background" and job.background_id:
        background = db.get(Background, job.background_id)
        if background is None:
            raise HTTPException(status_code=404, detail="Hintergrund wurde nicht gefunden")
        object_key = background.object_key
        content_type = background.content_type
    else:
        raise HTTPException(status_code=404, detail="Korrekturdatei wurde nicht gefunden")
    return Response(
        content=storage.get_object(object_key=object_key),
        media_type=content_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/photos/{photo_id}/correction")
def save_window_correction(
    photo_id: uuid.UUID,
    request: Request,
    mask: UploadFile = File(),
    background_shift_percent: int = Form(default=14),
    refine_edges: bool = Form(default=False),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_quality_operator(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    photo, job, _, _ = _window_correction_photo(db, admin, photo_id)
    if not 0 <= background_shift_percent <= 35:
        raise HTTPException(status_code=400, detail="Ungültige Hintergrundposition")
    content = mask.file.read(MAX_CONFIGURATION_IMAGE_BYTES + 1)
    if len(content) > MAX_CONFIGURATION_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Die Hintergrundmaske ist zu groß")
    try:
        image = Image.open(io.BytesIO(content)).convert("RGBA")
        image.load()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Die Hintergrundmaske ist ungültig") from exc
    if image.getchannel("A").getbbox() is None:
        raise HTTPException(status_code=400, detail="Die Hintergrundmaske ist leer")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    mask_content = output.getvalue()
    mask_key = (
        f"dealerships/{job.dealership_id}/jobs/{job.id}/"
        f"photos/{photo.id}/window-mask-manual-{uuid.uuid4()}.png"
    )
    storage.put_object(
        object_key=mask_key,
        content=mask_content,
        content_type="image/png",
    )
    photo.window_mask_object_key = mask_key
    photo.window_mask_is_manual = True
    # GrabCut is deliberately deferred to the image worker. Running it in this
    # request can exceed the reverse-proxy timeout for full-resolution photos.
    photo.window_mask_refine_edges = refine_edges
    photo.window_background_shift_percent = background_shift_percent
    photo.quality_review_required = True
    photo.quality_review_reason = (
        "Das nachbearbeitete Ergebnis wartet auf die manuelle Operator-Freigabe."
    )
    # This photo is already in the operator queue. Keeping the original review
    # timestamp prevents a correction from being announced as a new review.
    if photo.quality_review_created_at is None:
        photo.quality_review_created_at = datetime.now(timezone.utc)
    photo.quality_reviewed_by_id = None
    photo.quality_reviewed_at = None
    photo.quality_review_resolution = "correction_processing"
    photo.processing_status = ProcessingStatus.QUEUED
    photo.processing_error = None
    job.status = JobStatus.PROCESSING
    db.commit()
    try:
        enqueue_photo_processing(photo.id)
    except ProcessingQueueUnavailable:
        photo.processing_status = ProcessingStatus.FAILED
        photo.processing_error = "Verarbeitungswarteschlange ist nicht erreichbar"
        job.status = JobStatus.REVIEW_REQUIRED
        db.commit()
        _flash(request, "Die Korrektur konnte nicht verarbeitet werden.", "error")
    else:
        _flash(request, "Die Korrektur wird jetzt neu verarbeitet.")
    redirect_url = "/admin/quality-reviews"
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"status": "queued", "redirect": redirect_url})
    return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/exports")
def create_vehicle_export(
    job_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    job = db.get(VehicleJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    _authorized_dealership(db, admin, job.dealership_id)
    active_export = db.scalar(
        select(ExportRun).where(
            ExportRun.vehicle_job_id == job.id,
            ExportRun.status.in_(["queued", "processing"]),
        )
    )
    if active_export is not None:
        _flash(request, "Für diesen Auftrag wird bereits eine ZIP-Datei erstellt.", "error")
        return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)
    try:
        resolve_export_items(db, job)
    except ExportValidationError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    attempt = (
        db.scalar(select(func.max(ExportRun.attempt)).where(ExportRun.vehicle_job_id == job.id))
        or 0
    ) + 1
    export_run = ExportRun(
        vehicle_job_id=job.id,
        attempt=attempt,
        zip_filename=f"{safe_vin(job.vin)}.zip",
        status="queued",
    )
    db.add(export_run)
    db.commit()
    db.refresh(export_run)
    try:
        enqueue_vehicle_export(export_run.id)
    except ProcessingQueueUnavailable:
        export_run.status = "failed"
        export_run.error_message = "Verarbeitungswarteschlange ist nicht erreichbar"
        db.commit()
        _flash(request, "Die ZIP-Erstellung konnte nicht gestartet werden.", "error")
    else:
        _flash(request, "Die ZIP-Datei wird im Hintergrund erstellt.")
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/exports/{export_run_id}/transfer")
def transfer_vehicle_export(
    export_run_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    export_run = db.get(ExportRun, export_run_id)
    if export_run is None:
        raise HTTPException(status_code=404, detail="Export wurde nicht gefunden")
    job = db.get(VehicleJob, export_run.vehicle_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Auftrag wurde nicht gefunden")
    _authorized_dealership(db, admin, job.dealership_id)
    config = db.get(DealershipSftpSettings, job.dealership_id)
    if export_run.status != "completed" or not export_run.object_key:
        _flash(request, "Die ZIP-Datei ist noch nicht verfügbar.", "error")
    elif export_run.transfer_status in {"queued", "processing"}:
        _flash(request, "Diese ZIP-Datei wird bereits übertragen.", "error")
    elif config is None or not config.is_enabled:
        _flash(request, "Die SFTP-Übertragung ist für dieses Autohaus nicht aktiviert.", "error")
    else:
        export_run.transfer_status = "queued"
        export_run.transfer_error = None
        db.commit()
        try:
            enqueue_export_transfer(export_run.id)
        except ProcessingQueueUnavailable:
            export_run.transfer_status = "failed"
            export_run.transfer_error = "Verarbeitungswarteschlange ist nicht erreichbar"
            db.commit()
            _flash(request, "Die Übertragung konnte nicht gestartet werden.", "error")
        else:
            _flash(request, "Die SFTP-Übertragung wurde gestartet.")
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/dealerships/{dealership_id}/configuration", response_class=HTMLResponse)
def configuration_page(
    dealership_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    dealership = _authorized_dealership(db, admin, dealership_id)
    orientations = _ensure_standard_orientations(db)
    brands = list(
        db.scalars(select(Brand).where(Brand.dealership_id == dealership.id).order_by(Brand.name))
    )
    locations = list(
        db.scalars(
            select(Location).where(Location.dealership_id == dealership.id).order_by(Location.name)
        )
    )
    backgrounds = list(
        db.scalars(
            select(Background)
            .options(selectinload(Background.locations))
            .where(Background.dealership_id == dealership.id)
            .order_by(Background.name)
        )
    )
    background_ids = [background.id for background in backgrounds]
    composition_overrides = (
        list(
            db.scalars(
                select(BackgroundOrientationComposition).where(
                    BackgroundOrientationComposition.background_id.in_(background_ids)
                )
            )
        )
        if background_ids
        else []
    )
    overlays = list(
        db.scalars(
            select(ImageOverlay)
            .options(
                selectinload(ImageOverlay.locations),
                selectinload(ImageOverlay.capture_steps),
            )
            .where(ImageOverlay.dealership_id == dealership.id)
            .order_by(ImageOverlay.name)
        )
    )
    supplemental_images = list(
        db.scalars(
            select(SupplementalImage)
            .options(selectinload(SupplementalImage.locations))
            .where(SupplementalImage.dealership_id == dealership.id)
            .order_by(SupplementalImage.export_order, SupplementalImage.name)
        )
    )
    steps = list(
        db.scalars(
            select(CaptureStep)
            .where(CaptureStep.dealership_id == dealership.id)
            .order_by(CaptureStep.capture_order, CaptureStep.name)
        )
    )
    default_overlay_step = min(
        (step for step in steps if step.export_order is not None and step.is_active),
        key=lambda step: (step.export_order, step.capture_order, step.name),
        default=None,
    )
    background_previews = {
        background.id: storage.create_download_url(object_key=background.object_key)
        for background in backgrounds
    }
    overlay_previews = {
        overlay.id: storage.create_download_url(object_key=overlay.object_key)
        for overlay in overlays
    }
    supplemental_previews = {
        item.id: storage.create_download_url(object_key=item.object_key)
        for item in supplemental_images
    }
    silhouette_previews = {
        step.id: storage.create_download_url(object_key=step.silhouette_object_key)
        for step in steps
        if step.silhouette_object_key
    }
    return templates.TemplateResponse(
        request,
        "admin/configuration.html",
        _context(
            request,
            admin,
            dealership=dealership,
            brands=brands,
            locations=locations,
            backgrounds=backgrounds,
            overlays=overlays,
            supplemental_images=supplemental_images,
            steps=steps,
            brand_by_id={brand.id: brand for brand in brands},
            background_previews=background_previews,
            overlay_previews=overlay_previews,
            supplemental_previews=supplemental_previews,
            silhouette_previews=silhouette_previews,
            overlay_positions=OVERLAY_POSITIONS,
            default_overlay_step=default_overlay_step,
            orientations=orientations,
            composition_orientations=[
                orientation
                for orientation in orientations
                if orientation.is_active
                and orientation.processing_mode in {"optimized", "configurable"}
            ],
            composition_overrides_by_background={
                background.id: {
                    override.orientation_id: override
                    for override in composition_overrides
                    if override.background_id == background.id
                }
                for background in backgrounds
            },
            steps_by_orientation_id={
                orientation.id: sorted(
                    [step for step in steps if step.orientation_id == orientation.id],
                    key=lambda step: step.orientation_instance_index,
                )
                for orientation in orientations
            },
        ),
    )


@router.post("/dealerships/{dealership_id}/brands")
def create_brand(
    dealership_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    cleaned_name = name.strip()
    if not cleaned_name:
        _flash(request, "Bitte geben Sie einen Markennamen ein.", "error")
    else:
        db.add(Brand(dealership_id=dealership.id, name=cleaned_name))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _flash(request, "Diese Marke ist bereits vorhanden.", "error")
        else:
            _flash(request, "Marke wurde angelegt.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#brands",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/brands/{brand_id}")
def update_brand(
    brand_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    brand = db.get(Brand, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail="Marke wurde nicht gefunden")
    _authorized_dealership(db, admin, brand.dealership_id)
    cleaned_name = name.strip()
    if not cleaned_name:
        _flash(request, "Bitte geben Sie einen Markennamen ein.", "error")
    else:
        brand.name = cleaned_name
        brand.is_active = is_active == "on"
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _flash(request, "Diese Marke ist bereits vorhanden.", "error")
        else:
            _flash(request, "Marke wurde gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{brand.dealership_id}/configuration#brands",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dealerships/{dealership_id}/backgrounds")
async def create_background(
    dealership_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    brand_id: str = Form(default=""),
    location_ids: list[uuid.UUID] = Form(default=[]),
    image: UploadFile = File(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    cleaned_name = name.strip()
    if not cleaned_name:
        _flash(request, "Bitte geben Sie einen Hintergrundnamen ein.", "error")
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}/configuration#backgrounds",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    selected_brand = _tenant_brand(db, dealership.id, _optional_uuid(brand_id))
    selected_locations = _tenant_locations(db, dealership.id, location_ids)
    object_key, content_type = await _store_configuration_image(
        storage,
        image,
        object_key_prefix=f"dealerships/{dealership.id}/configuration/backgrounds",
    )
    background = Background(
        dealership_id=dealership.id,
        brand_id=selected_brand.id if selected_brand else None,
        name=cleaned_name,
        object_key=object_key,
        content_type=content_type,
        locations=selected_locations,
    )
    db.add(background)
    db.commit()
    _flash(request, "Hintergrund wurde hochgeladen.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#backgrounds",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/backgrounds/{background_id}")
def update_background(
    background_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    brand_id: str = Form(default=""),
    location_ids: list[uuid.UUID] = Form(default=[]),
    contour_target_area_percent: int = Form(default=36),
    contour_max_width_percent: int = Form(default=78),
    contour_max_height_percent: int = Form(default=72),
    vehicle_bottom_percent: int = Form(default=90),
    shadow_opacity_percent: int = Form(default=32),
    reflection_opacity_percent: int = Form(default=10),
    brightness_percent: int = Form(default=100),
    window_background_shift_percent: int = Form(default=14),
    scene_projection_enabled: str | None = Form(default=None),
    scene_horizon_percent: int = Form(default=43),
    scene_reference_vertical_degrees: int = Form(default=0),
    scene_perspective_strength_percent: int = Form(default=35),
    composition_orientation_ids: list[uuid.UUID] = Form(default=[]),
    custom_composition_orientation_ids: list[uuid.UUID] = Form(default=[]),
    orientation_target_area_percents: list[str] = Form(default=[]),
    orientation_max_width_percents: list[str] = Form(default=[]),
    orientation_max_height_percents: list[str] = Form(default=[]),
    orientation_bottom_percents: list[str] = Form(default=[]),
    orientation_shadow_percents: list[str] = Form(default=[]),
    orientation_reflection_percents: list[str] = Form(default=[]),
    orientation_brightness_percents: list[str] = Form(default=[]),
    orientation_window_shift_percents: list[str] = Form(default=[]),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    background = db.get(Background, background_id)
    if background is None:
        raise HTTPException(status_code=404, detail="Hintergrund wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, background.dealership_id)
    cleaned_name = name.strip()
    values_valid = (
        15 <= contour_target_area_percent <= 60
        and 40 <= contour_max_width_percent <= 95
        and 40 <= contour_max_height_percent <= 90
        and 55 <= vehicle_bottom_percent <= 98
        and 0 <= shadow_opacity_percent <= 80
        and 0 <= reflection_opacity_percent <= 60
        and 50 <= brightness_percent <= 150
        and 0 <= window_background_shift_percent <= 35
        and 25 <= scene_horizon_percent <= 70
        and -30 <= scene_reference_vertical_degrees <= 30
        and 0 <= scene_perspective_strength_percent <= 100
    )
    if not orientation_window_shift_percents and composition_orientation_ids:
        orientation_window_shift_percents = [""] * len(composition_orientation_ids)
    override_value_lists = [
        orientation_target_area_percents,
        orientation_max_width_percents,
        orientation_max_height_percents,
        orientation_bottom_percents,
        orientation_shadow_percents,
        orientation_reflection_percents,
        orientation_brightness_percents,
        orientation_window_shift_percents,
    ]
    overrides_well_formed = all(
        len(values) == len(composition_orientation_ids) for values in override_value_lists
    )

    def optional_number(raw: str, minimum: int, maximum: int) -> int | None:
        if not raw.strip():
            return None
        value = int(raw)
        if not minimum <= value <= maximum:
            raise ValueError
        return value

    parsed_overrides: dict[uuid.UUID, tuple[int | None, ...]] = {}
    if overrides_well_formed:
        try:
            for index, orientation_id in enumerate(composition_orientation_ids):
                parsed_overrides[orientation_id] = (
                    optional_number(orientation_target_area_percents[index], 15, 60),
                    optional_number(orientation_max_width_percents[index], 40, 95),
                    optional_number(orientation_max_height_percents[index], 40, 90),
                    optional_number(orientation_bottom_percents[index], 55, 98),
                    optional_number(orientation_shadow_percents[index], 0, 80),
                    optional_number(orientation_reflection_percents[index], 0, 60),
                    optional_number(orientation_brightness_percents[index], 50, 150),
                    optional_number(orientation_window_shift_percents[index], 0, 35),
                )
        except (TypeError, ValueError):
            overrides_well_formed = False

    if not cleaned_name or not values_valid or not overrides_well_formed:
        _flash(request, "Bitte prüfen Sie Name und Showroom-Einstellungen.", "error")
    else:
        selected_brand = _tenant_brand(db, dealership.id, _optional_uuid(brand_id))
        background.name = cleaned_name
        background.brand_id = selected_brand.id if selected_brand else None
        background.locations = _tenant_locations(db, dealership.id, location_ids)
        background.contour_target_area_percent = contour_target_area_percent
        background.contour_max_width_percent = contour_max_width_percent
        background.contour_max_height_percent = contour_max_height_percent
        background.vehicle_bottom_percent = vehicle_bottom_percent
        background.shadow_opacity_percent = shadow_opacity_percent
        background.reflection_opacity_percent = reflection_opacity_percent
        background.brightness_percent = brightness_percent
        background.window_background_shift_percent = window_background_shift_percent
        background.scene_projection_enabled = scene_projection_enabled == "on"
        background.scene_horizon_percent = scene_horizon_percent
        background.scene_reference_vertical_degrees = scene_reference_vertical_degrees
        background.scene_perspective_strength_percent = scene_perspective_strength_percent
        background.is_active = is_active == "on"

        valid_orientations = {
            orientation.id
            for orientation in db.scalars(
                select(Orientation).where(
                    Orientation.id.in_(composition_orientation_ids),
                    Orientation.processing_mode.in_({"optimized", "configurable"}),
                )
            )
        }
        custom_ids = set(custom_composition_orientation_ids) & valid_orientations
        existing_overrides = {
            override.orientation_id: override
            for override in db.scalars(
                select(BackgroundOrientationComposition).where(
                    BackgroundOrientationComposition.background_id == background.id
                )
            )
        }
        for orientation_id in valid_orientations:
            existing = existing_overrides.get(orientation_id)
            if orientation_id not in custom_ids:
                if existing is not None:
                    db.delete(existing)
                continue
            values = parsed_overrides[orientation_id]
            override = existing or BackgroundOrientationComposition(
                background_id=background.id,
                orientation_id=orientation_id,
            )
            (
                override.contour_target_area_percent,
                override.contour_max_width_percent,
                override.contour_max_height_percent,
                override.vehicle_bottom_percent,
                override.shadow_opacity_percent,
                override.reflection_opacity_percent,
                override.brightness_percent,
                override.window_background_shift_percent,
            ) = values
            if existing is None:
                db.add(override)
        db.commit()
        _flash(request, "Hintergrund wurde gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#backgrounds",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/backgrounds/{background_id}/delete")
def delete_background(
    background_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    background = db.get(Background, background_id)
    if background is None:
        raise HTTPException(status_code=404, detail="Hintergrund wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, background.dealership_id)
    db.execute(
        update(VehicleJob)
        .where(VehicleJob.background_id == background.id)
        .values(background_id=None)
    )
    db.delete(background)
    try:
        db.flush()
        storage.delete_object(object_key=background.object_key)
        db.commit()
    except StorageUnavailableError:
        db.rollback()
        _flash(
            request,
            "Der Bildspeicher ist nicht erreichbar. Der Hintergrund wurde nicht gelöscht.",
            "error",
        )
    except IntegrityError:
        db.rollback()
        _flash(request, "Der Hintergrund konnte nicht gelöscht werden.", "error")
    else:
        _flash(request, "Hintergrund wurde dauerhaft gelöscht.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#backgrounds",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dealerships/{dealership_id}/overlays")
async def create_overlay(
    dealership_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    brand_id: str = Form(default=""),
    location_ids: list[uuid.UUID] = Form(default=[]),
    capture_step_ids: list[uuid.UUID] = Form(default=[]),
    position: str = Form(default="bottom_right"),
    width_percent: int = Form(default=18),
    opacity_percent: int = Form(default=100),
    image: UploadFile = File(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    cleaned_name = name.strip()
    if (
        not cleaned_name
        or position not in OVERLAY_POSITIONS
        or not 5 <= width_percent <= 60
        or not 10 <= opacity_percent <= 100
    ):
        _flash(request, "Bitte prüfen Sie Name, Position, Größe und Deckkraft.", "error")
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}/configuration#overlays",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    selected_brand = _tenant_brand(db, dealership.id, _optional_uuid(brand_id))
    selected_locations = _tenant_locations(db, dealership.id, location_ids)
    selected_steps = _overlay_capture_steps(db, dealership.id, capture_step_ids)
    object_key, content_type = await _store_configuration_image(
        storage,
        image,
        object_key_prefix=f"dealerships/{dealership.id}/configuration/overlays",
        png_only=True,
    )
    db.add(
        ImageOverlay(
            dealership_id=dealership.id,
            brand_id=selected_brand.id if selected_brand else None,
            name=cleaned_name,
            object_key=object_key,
            content_type=content_type,
            position=position,
            width_percent=width_percent,
            opacity_percent=opacity_percent,
            locations=selected_locations,
            capture_steps=selected_steps,
        )
    )
    db.commit()
    _flash(request, "Overlay wurde hochgeladen.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#overlays",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/overlays/{overlay_id}")
def update_overlay(
    overlay_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    brand_id: str = Form(default=""),
    location_ids: list[uuid.UUID] = Form(default=[]),
    capture_step_ids: list[uuid.UUID] = Form(default=[]),
    position: str = Form(default="bottom_right"),
    width_percent: int = Form(default=18),
    opacity_percent: int = Form(default=100),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    overlay = db.get(ImageOverlay, overlay_id)
    if overlay is None:
        raise HTTPException(status_code=404, detail="Overlay wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, overlay.dealership_id)
    cleaned_name = name.strip()
    if (
        not cleaned_name
        or position not in OVERLAY_POSITIONS
        or not 5 <= width_percent <= 60
        or not 10 <= opacity_percent <= 100
    ):
        _flash(request, "Bitte prüfen Sie Name, Position, Größe und Deckkraft.", "error")
    else:
        selected_brand = _tenant_brand(db, dealership.id, _optional_uuid(brand_id))
        overlay.name = cleaned_name
        overlay.brand_id = selected_brand.id if selected_brand else None
        overlay.locations = _tenant_locations(db, dealership.id, location_ids)
        overlay.capture_steps = _overlay_capture_steps(db, dealership.id, capture_step_ids)
        overlay.position = position
        overlay.width_percent = width_percent
        overlay.opacity_percent = opacity_percent
        overlay.is_active = is_active == "on"
        db.commit()
        _flash(request, "Overlay wurde gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#overlays",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/overlays/{overlay_id}/delete")
def delete_overlay(
    overlay_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    overlay = db.get(ImageOverlay, overlay_id)
    if overlay is None:
        raise HTTPException(status_code=404, detail="Overlay wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, overlay.dealership_id)
    db.delete(overlay)
    try:
        db.flush()
        storage.delete_object(object_key=overlay.object_key)
        db.commit()
    except StorageUnavailableError:
        db.rollback()
        _flash(
            request,
            "Der Bildspeicher ist nicht erreichbar. Das Overlay wurde nicht gelöscht.",
            "error",
        )
    except IntegrityError:
        db.rollback()
        _flash(request, "Das Overlay konnte nicht gelöscht werden.", "error")
    else:
        _flash(request, "Overlay wurde dauerhaft gelöscht.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#overlays",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dealerships/{dealership_id}/supplemental-images")
async def create_supplemental_image(
    dealership_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    export_order: int = Form(),
    brand_id: str = Form(default=""),
    location_ids: list[uuid.UUID] = Form(default=[]),
    image: UploadFile = File(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    cleaned_name = name.strip()
    if not cleaned_name or export_order < 1:
        _flash(request, "Bitte prüfen Sie Name und Export-Nr.", "error")
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}/configuration#supplemental-images",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    selected_brand = _tenant_brand(db, dealership.id, _optional_uuid(brand_id))
    selected_locations = _tenant_locations(db, dealership.id, location_ids)
    conflict = _supplemental_export_order_conflict(
        db,
        dealership.id,
        export_order,
        selected_brand.id if selected_brand else None,
        selected_locations,
    )
    if conflict:
        _flash(
            request,
            f"Exportplatz {export_order} ist bereits durch „{conflict}“ belegt.",
            "error",
        )
        return RedirectResponse(
            f"/admin/dealerships/{dealership.id}/configuration#supplemental-images",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    object_key, content_type = await _store_configuration_image(
        storage,
        image,
        object_key_prefix=f"dealerships/{dealership.id}/configuration/supplemental-images",
    )
    db.add(
        SupplementalImage(
            dealership_id=dealership.id,
            brand_id=selected_brand.id if selected_brand else None,
            name=cleaned_name,
            object_key=object_key,
            content_type=content_type,
            export_order=export_order,
            locations=selected_locations,
        )
    )
    db.commit()
    _flash(request, "Zusatzbild wurde hochgeladen.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#supplemental-images",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/supplemental-images/{image_id}")
def update_supplemental_image(
    image_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    export_order: int = Form(),
    brand_id: str = Form(default=""),
    location_ids: list[uuid.UUID] = Form(default=[]),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    supplemental_image = db.get(SupplementalImage, image_id)
    if supplemental_image is None:
        raise HTTPException(status_code=404, detail="Zusatzbild wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, supplemental_image.dealership_id)
    cleaned_name = name.strip()
    if not cleaned_name or export_order < 1:
        _flash(request, "Bitte prüfen Sie Name und Export-Nr.", "error")
    else:
        selected_brand = _tenant_brand(db, dealership.id, _optional_uuid(brand_id))
        selected_locations = _tenant_locations(db, dealership.id, location_ids)
        conflict = (
            _supplemental_export_order_conflict(
                db,
                dealership.id,
                export_order,
                selected_brand.id if selected_brand else None,
                selected_locations,
                excluding_image_id=supplemental_image.id,
            )
            if is_active == "on"
            else None
        )
        if conflict:
            _flash(
                request,
                f"Exportplatz {export_order} ist bereits durch „{conflict}“ belegt.",
                "error",
            )
        else:
            supplemental_image.name = cleaned_name
            supplemental_image.export_order = export_order
            supplemental_image.brand_id = selected_brand.id if selected_brand else None
            supplemental_image.locations = selected_locations
            supplemental_image.is_active = is_active == "on"
            db.commit()
            _flash(request, "Zusatzbild wurde gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#supplemental-images",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/supplemental-images/{image_id}/delete")
def delete_supplemental_image(
    image_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    supplemental_image = db.get(SupplementalImage, image_id)
    if supplemental_image is None:
        raise HTTPException(status_code=404, detail="Zusatzbild wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, supplemental_image.dealership_id)
    db.delete(supplemental_image)
    try:
        db.flush()
        storage.delete_object(object_key=supplemental_image.object_key)
        db.commit()
    except StorageUnavailableError:
        db.rollback()
        _flash(
            request,
            "Der Bildspeicher ist nicht erreichbar. Das Zusatzbild wurde nicht gelöscht.",
            "error",
        )
    except IntegrityError:
        db.rollback()
        _flash(request, "Das Zusatzbild konnte nicht gelöscht werden.", "error")
    else:
        _flash(request, "Zusatzbild wurde dauerhaft gelöscht.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#supplemental-images",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dealerships/{dealership_id}/capture-steps/defaults")
def create_default_capture_steps(
    dealership_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    orientations = _ensure_standard_orientations(db)
    existing_orientation_instances = set(
        db.execute(
            select(CaptureStep.orientation_id, CaptureStep.orientation_instance_index).where(
                CaptureStep.dealership_id == dealership.id,
                CaptureStep.orientation_id.is_not(None),
            )
        )
    )
    next_order = (
        db.scalar(
            select(func.max(CaptureStep.capture_order)).where(
                CaptureStep.dealership_id == dealership.id
            )
        )
        or 0
    )
    used_export_orders = set(
        db.scalars(
            select(CaptureStep.export_order).where(
                CaptureStep.dealership_id == dealership.id,
                CaptureStep.export_order.is_not(None),
            )
        )
    )
    next_export_order = 0
    added = 0
    for orientation in orientations:
        for instance_index in range(1, orientation.default_instance_count + 1):
            if (orientation.id, instance_index) in existing_orientation_instances:
                continue
            next_order += 1
            next_export_order += 1
            while next_export_order in used_export_orders:
                next_export_order += 1
            used_export_orders.add(next_export_order)
            db.add(
                CaptureStep(
                    dealership_id=dealership.id,
                    orientation_id=orientation.id,
                    orientation_instance_index=instance_index,
                    name=instance_name(orientation.name, instance_index, orientation.is_repeatable),
                    instruction=orientation.instruction,
                    category=orientation.category,
                    capture_order=next_order,
                    export_order=next_export_order,
                    is_required=orientation.is_required,
                    requires_processing=orientation.requires_processing,
                    is_active=orientation.is_active,
                    silhouette_object_key=orientation.silhouette_object_key,
                    silhouette_content_type=orientation.silhouette_content_type,
                )
            )
            added += 1
    db.commit()
    _flash(
        request,
        f"{added} Standard-Fotopositionen wurden ergänzt."
        if added
        else "Der Standardablauf ist bereits vollständig vorhanden.",
    )
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#orientations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dealerships/{dealership_id}/orientation-settings")
def update_dealership_orientation_settings(
    dealership_id: uuid.UUID,
    request: Request,
    orientation_ids: list[uuid.UUID] = Form(),
    capture_orders: list[int] = Form(),
    export_orders: list[str] = Form(),
    instance_counts: list[int] = Form(default=[]),
    active_orientation_ids: list[uuid.UUID] = Form(default=[]),
    required_orientation_ids: list[uuid.UUID] = Form(default=[]),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    if not instance_counts:
        instance_counts = [1] * len(orientation_ids)
    if not (
        len(orientation_ids) == len(capture_orders) == len(export_orders) == len(instance_counts)
        and len(set(orientation_ids)) == len(orientation_ids)
    ):
        raise HTTPException(status_code=400, detail="Ungültige Orientierungsauswahl")
    orientations = {
        item.id: item
        for item in db.scalars(select(Orientation).where(Orientation.id.in_(orientation_ids)))
    }
    if len(orientations) != len(orientation_ids):
        raise HTTPException(status_code=400, detail="Ungültige Orientierungsauswahl")
    active_ids = set(active_orientation_ids)
    required_ids = set(required_orientation_ids)
    parsed_export_orders: list[int | None] = []
    for raw_value in export_orders:
        try:
            parsed_export_orders.append(int(raw_value) if raw_value.strip() else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ungültige Exportreihenfolge") from exc
    invalid_count = next(
        (
            orientation_id
            for orientation_id, count in zip(orientation_ids, instance_counts, strict=True)
            if count < 1
            or count > orientations[orientation_id].max_instances
            or (not orientations[orientation_id].is_repeatable and count != 1)
        ),
        None,
    )
    active_rows = []
    for orientation_id, capture_order, export_order, count in zip(
        orientation_ids,
        capture_orders,
        parsed_export_orders,
        instance_counts,
        strict=True,
    ):
        if orientation_id not in active_ids:
            continue
        for instance_index in range(1, count + 1):
            active_rows.append(
                (
                    orientation_id,
                    instance_index,
                    capture_order + instance_index - 1,
                    export_order + instance_index - 1 if export_order is not None else None,
                )
            )
    if invalid_count is not None:
        _flash(request, "Bitte prüfen Sie die Anzahl der wiederholbaren Aufnahmen.", "error")
    elif any(not orientations[item_id].is_active for item_id, _, _, _ in active_rows):
        _flash(
            request, "Eine zentral deaktivierte Orientierung kann nicht aktiviert werden.", "error"
        )
    elif any(order < 1 for _, _, order, _ in active_rows) or any(
        export_order is not None and export_order < 1 for _, _, _, export_order in active_rows
    ):
        _flash(request, "Bitte prüfen Sie Aufnahme- und Exportreihenfolge.", "error")
    elif len({order for _, _, order, _ in active_rows}) != len(active_rows):
        _flash(request, "Jede aktive Orientierung benötigt einen eigenen Aufnahmeplatz.", "error")
    elif len(
        {export_order for _, _, _, export_order in active_rows if export_order is not None}
    ) != len([1 for _, _, _, export_order in active_rows if export_order is not None]):
        _flash(request, "Jede aktive Orientierung benötigt einen eigenen Exportplatz.", "error")
    else:
        submitted_ids = set(orientation_ids)
        untouched_steps = [
            step
            for step in db.scalars(
                select(CaptureStep).where(
                    CaptureStep.dealership_id == dealership.id,
                    CaptureStep.is_active.is_(True),
                )
            )
            if step.orientation_id not in submitted_ids
        ]
        untouched_capture_orders = {step.capture_order: step.name for step in untouched_steps}
        untouched_export_orders = {
            step.export_order: step.name
            for step in untouched_steps
            if step.export_order is not None
        }
        capture_conflict = next(
            (
                (capture_order, untouched_capture_orders[capture_order])
                for _, _, capture_order, _ in active_rows
                if capture_order in untouched_capture_orders
            ),
            None,
        )
        export_conflict = next(
            (
                (export_order, untouched_export_orders[export_order])
                for _, _, _, export_order in active_rows
                if export_order is not None and export_order in untouched_export_orders
            ),
            None,
        )
        if capture_conflict is not None:
            _flash(
                request,
                f"Aufnahmeplatz {capture_conflict[0]} ist bereits durch "
                f"„{capture_conflict[1]}“ belegt.",
                "error",
            )
        elif export_conflict is not None:
            _flash(
                request,
                f"Exportplatz {export_conflict[0]} ist bereits durch "
                f"„{export_conflict[1]}“ belegt.",
                "error",
            )
        else:
            assigned_steps = list(
                db.scalars(
                    select(CaptureStep).where(
                        CaptureStep.dealership_id == dealership.id,
                        CaptureStep.orientation_id.in_(orientation_ids),
                    )
                )
            )
            existing_steps = {
                (step.orientation_id, step.orientation_instance_index): step
                for step in assigned_steps
            }
            active_instance_keys = {
                (orientation_id, instance_index)
                for orientation_id, instance_index, _, _ in active_rows
            }
            for step in assigned_steps:
                if (
                    step.orientation_id,
                    step.orientation_instance_index,
                ) not in active_instance_keys:
                    step.is_active = False
            for orientation_id, instance_index, capture_order, export_order in active_rows:
                orientation = orientations[orientation_id]
                step = existing_steps.get((orientation_id, instance_index))
                if step is None:
                    step = CaptureStep(
                        dealership_id=dealership.id,
                        orientation_id=orientation.id,
                        orientation_instance_index=instance_index,
                        name=instance_name(
                            orientation.name, instance_index, orientation.is_repeatable
                        ),
                        instruction=orientation.instruction,
                        category=orientation.category,
                        requires_processing=orientation.requires_processing,
                        silhouette_object_key=orientation.silhouette_object_key,
                        silhouette_content_type=orientation.silhouette_content_type,
                    )
                    db.add(step)
                else:
                    step.name = instance_name(
                        orientation.name, instance_index, orientation.is_repeatable
                    )
                    step.instruction = orientation.instruction
                    step.category = orientation.category
                    if orientation.processing_mode != "configurable":
                        step.requires_processing = orientation.requires_processing
                step.capture_order = capture_order
                step.export_order = export_order
                step.is_required = orientation_id in required_ids
                step.is_active = True
            db.commit()
            _flash(request, "App- und Exportreihenfolge wurden gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#orientations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dealerships/{dealership_id}/capture-steps")
def create_capture_step(
    dealership_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    instruction: str = Form(default=""),
    category: str = Form(default="detail"),
    capture_order: int = Form(),
    export_order: int | None = Form(default=None),
    is_required: str | None = Form(default=None),
    requires_processing: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    if category not in ORIENTATION_CATEGORIES | {"free"}:
        raise HTTPException(status_code=400, detail="Ungültige Kategorie")
    step = CaptureStep(
        dealership_id=dealership.id,
        name=name.strip(),
        instruction=instruction.strip(),
        category=category,
        capture_order=capture_order,
        export_order=export_order,
        is_required=is_required == "on",
        requires_processing=requires_processing == "on",
    )
    if not step.name or capture_order < 1 or (export_order is not None and export_order < 1):
        _flash(request, "Bitte prüfen Sie Name und Reihenfolge.", "error")
    elif conflict := _capture_order_conflict(db, dealership.id, capture_order):
        _flash(
            request,
            f"Aufnahmeplatz {capture_order} ist bereits durch „{conflict}“ belegt.",
            "error",
        )
    elif conflict := _capture_export_order_conflict(db, dealership.id, export_order):
        _flash(
            request,
            f"Exportplatz {export_order} ist bereits durch „{conflict}“ belegt.",
            "error",
        )
    else:
        db.add(step)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _flash(request, "Diese Fotoposition ist bereits vorhanden.", "error")
        else:
            _flash(request, "Fotoposition wurde angelegt.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#capture-steps",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/capture-steps/{step_id}")
async def update_capture_step(
    step_id: uuid.UUID,
    request: Request,
    name: str = Form(),
    instruction: str = Form(default=""),
    category: str = Form(default="detail"),
    capture_order: int = Form(),
    export_order: int | None = Form(default=None),
    is_required: str | None = Form(default=None),
    requires_processing: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    silhouette: UploadFile | None = File(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_object_storage),
):
    admin = _require_system_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    step = db.get(CaptureStep, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="Fotoposition wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, step.dealership_id)
    if category not in ORIENTATION_CATEGORIES | {"free"}:
        raise HTTPException(status_code=400, detail="Ungültige Kategorie")
    if not name.strip() or capture_order < 1 or (export_order is not None and export_order < 1):
        _flash(request, "Bitte prüfen Sie Name und Reihenfolge.", "error")
    elif is_active == "on" and (
        conflict := _capture_order_conflict(
            db, dealership.id, capture_order, excluding_step_id=step.id
        )
    ):
        _flash(
            request,
            f"Aufnahmeplatz {capture_order} ist bereits durch „{conflict}“ belegt.",
            "error",
        )
    elif conflict := _capture_export_order_conflict(
        db,
        dealership.id,
        export_order,
        excluding_step_id=step.id,
    ):
        _flash(
            request,
            f"Exportplatz {export_order} ist bereits durch „{conflict}“ belegt.",
            "error",
        )
    else:
        step.name = name.strip()
        step.instruction = instruction.strip()
        step.category = category
        step.capture_order = capture_order
        step.export_order = export_order
        step.is_required = is_required == "on"
        step.requires_processing = requires_processing == "on"
        step.is_active = is_active == "on"
        if silhouette is not None and silhouette.filename:
            object_key, content_type = await _store_configuration_image(
                storage,
                silhouette,
                object_key_prefix=f"dealerships/{dealership.id}/configuration/silhouettes",
                png_only=True,
            )
            step.silhouette_object_key = object_key
            step.silhouette_content_type = content_type
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _flash(request, "Diese Fotoposition ist bereits vorhanden.", "error")
        else:
            _flash(request, "Fotoposition wurde gespeichert.")
    return RedirectResponse(
        f"/admin/dealerships/{dealership.id}/configuration#capture-steps",
        status_code=status.HTTP_303_SEE_OTHER,
    )
