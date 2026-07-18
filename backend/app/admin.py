import secrets
import uuid
from datetime import datetime, timezone
from hmac import compare_digest
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
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
    Brand,
    CaptureStep,
    Dealership,
    ImageOverlay,
    JobStatus,
    Location,
    PhotoAsset,
    PhotoProcessingVariant,
    ProcessingStatus,
    RefreshSession,
    SupplementalImage,
    User,
    UserRole,
    VehicleCreditGrant,
    VehicleJob,
)
from app.processing_queue import (
    ProcessingQueueUnavailable,
    enqueue_photo_processing,
    enqueue_photo_variant,
)
from app.security import hash_password, verify_password
from app.storage import ObjectStorage, StorageUnavailableError, get_object_storage

router = APIRouter(prefix="/admin", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

MAX_CONFIGURATION_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png"}
OVERLAY_POSITIONS = {
    "top_left": "Oben links",
    "top_right": "Oben rechts",
    "bottom_left": "Unten links",
    "bottom_right": "Unten rechts",
    "center": "Mittig",
}
STANDARD_CAPTURE_STEPS = [
    ("Front", "Fahrzeug gerade und vollständig von vorne aufnehmen.", "exterior", True),
    ("Diagonal vorne links", "Vordere linke Fahrzeugecke vollständig zeigen.", "exterior", True),
    ("Seite links", "Linke Fahrzeugseite gerade und vollständig aufnehmen.", "exterior", True),
    ("Diagonal hinten links", "Hintere linke Fahrzeugecke vollständig zeigen.", "exterior", True),
    ("Heck", "Fahrzeug gerade und vollständig von hinten aufnehmen.", "exterior", True),
    ("Diagonal hinten rechts", "Hintere rechte Fahrzeugecke vollständig zeigen.", "exterior", True),
    ("Seite rechts", "Rechte Fahrzeugseite gerade und vollständig aufnehmen.", "exterior", True),
    ("Diagonal vorne rechts", "Vordere rechte Fahrzeugecke vollständig zeigen.", "exterior", True),
    ("Innenraum", "Gesamteindruck des Innenraums aufnehmen.", "interior", False),
    ("Lenkrad", "Lenkrad mittig und ohne Spiegelungen aufnehmen.", "detail", False),
    ("Armaturenbrett", "Armaturenbrett vollständig und scharf aufnehmen.", "detail", False),
    (
        "Blick ins Fahrzeug links",
        "Seitlichen Einblick durch die linke Tür aufnehmen.",
        "interior",
        False,
    ),
    (
        "Blick ins Fahrzeug rechts",
        "Seitlichen Einblick durch die rechte Tür aufnehmen.",
        "interior",
        False,
    ),
    (
        "Rücksitzbank links",
        "Rücksitzbank von der linken Fahrzeugseite aufnehmen.",
        "interior",
        False,
    ),
    (
        "Rücksitzbank rechts",
        "Rücksitzbank von der rechten Fahrzeugseite aufnehmen.",
        "interior",
        False,
    ),
    ("Kofferraum", "Geöffneten Kofferraum vollständig aufnehmen.", "detail", False),
]


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
        or user.role not in {UserRole.SYSTEM_ADMIN, UserRole.DEALERSHIP_ADMIN}
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


def _tenant_brand(
    db: Session, dealership_id: uuid.UUID, brand_id: uuid.UUID | None
) -> Brand | None:
    if brand_id is None:
        return None
    brand = db.get(Brand, brand_id)
    if brand is None or brand.dealership_id != dealership_id:
        raise HTTPException(status_code=400, detail="Ungültige Markenauswahl")
    return brand


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
        or user.role not in {UserRole.SYSTEM_ADMIN, UserRole.DEALERSHIP_ADMIN}
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
    statement = select(Dealership).order_by(Dealership.name)
    if admin.role == UserRole.DEALERSHIP_ADMIN:
        statement = statement.where(Dealership.id == admin.dealership_id)
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


@router.get("/image-service", response_class=HTMLResponse)
def image_service_page(request: Request, db: Session = Depends(get_db)):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    if admin.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
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
            photoroom_key_configured=bool(runtime.photoroom_api_key),
            photoroom_key_is_sandbox=bool(
                runtime.photoroom_api_key and runtime.photoroom_api_key.startswith("sandbox_")
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
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    if admin.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    if provider not in IMAGE_PROVIDERS or not 0 <= default_monthly_vehicle_credits <= 10000:
        _flash(request, "Bitte prüfen Sie Bilddienstleister und Standardkontingent.", "error")
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
    admin = _require_admin(request, db)
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
        ),
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
    admin = _require_admin(request, db)
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
        if admin.role == UserRole.SYSTEM_ADMIN:
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
    admin = _require_admin(request, db)
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
    admin = _require_admin(request, db)
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
    admin = _require_admin(request, db)
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
    if role == UserRole.SYSTEM_ADMIN or len(password) < 16:
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
    admin = _require_admin(request, db)
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
        for location in db.scalars(select(Location).where(Location.dealership_id == dealership.id))
    }
    return templates.TemplateResponse(
        request,
        "admin/jobs.html",
        _context(
            request,
            admin,
            dealership=dealership,
            jobs=jobs,
            locations=locations,
        ),
    )


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
            original_urls={
                photo.id: storage.create_download_url(object_key=photo.original_object_key)
                for photo in photos
            },
            original_download_urls={
                photo.id: storage.create_download_url(
                    object_key=photo.original_object_key,
                    filename=f"{job.vin}_{index:02d}_Original.jpg",
                )
                for index, photo in enumerate(photos, start=1)
            },
            processed_urls={
                photo.id: storage.create_download_url(object_key=photo.processed_object_key)
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
            optimized_photoroom_urls={
                variant.photo_asset_id: storage.create_download_url(object_key=variant.object_key)
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
    admin = _require_admin(request, db)
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
    admin = _require_admin(request, db)
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
    admin = _require_admin(request, db)
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
    vehicle_scale_percent: int = Form(default=78),
    vehicle_bottom_percent: int = Form(default=90),
    shadow_opacity_percent: int = Form(default=32),
    reflection_opacity_percent: int = Form(default=10),
    brightness_percent: int = Form(default=100),
    is_active: str | None = Form(default=None),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    background = db.get(Background, background_id)
    if background is None:
        raise HTTPException(status_code=404, detail="Hintergrund wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, background.dealership_id)
    cleaned_name = name.strip()
    values_valid = (
        20 <= vehicle_scale_percent <= 95
        and 55 <= vehicle_bottom_percent <= 98
        and 0 <= shadow_opacity_percent <= 80
        and 0 <= reflection_opacity_percent <= 60
        and 50 <= brightness_percent <= 150
    )
    if not cleaned_name or not values_valid:
        _flash(request, "Bitte prüfen Sie Name und Showroom-Einstellungen.", "error")
    else:
        selected_brand = _tenant_brand(db, dealership.id, _optional_uuid(brand_id))
        background.name = cleaned_name
        background.brand_id = selected_brand.id if selected_brand else None
        background.locations = _tenant_locations(db, dealership.id, location_ids)
        background.vehicle_scale_percent = vehicle_scale_percent
        background.vehicle_bottom_percent = vehicle_bottom_percent
        background.shadow_opacity_percent = shadow_opacity_percent
        background.reflection_opacity_percent = reflection_opacity_percent
        background.brightness_percent = brightness_percent
        background.is_active = is_active == "on"
        db.commit()
        _flash(request, "Hintergrund wurde gespeichert.")
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
    selected_steps = _tenant_capture_steps(db, dealership.id, capture_step_ids)
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
        overlay.capture_steps = _tenant_capture_steps(db, dealership.id, capture_step_ids)
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
        supplemental_image.name = cleaned_name
        supplemental_image.export_order = export_order
        supplemental_image.brand_id = selected_brand.id if selected_brand else None
        supplemental_image.locations = _tenant_locations(db, dealership.id, location_ids)
        supplemental_image.is_active = is_active == "on"
        db.commit()
        _flash(request, "Zusatzbild wurde gespeichert.")
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
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    existing_names = set(
        db.scalars(select(CaptureStep.name).where(CaptureStep.dealership_id == dealership.id))
    )
    next_order = (
        db.scalar(
            select(func.max(CaptureStep.capture_order)).where(
                CaptureStep.dealership_id == dealership.id
            )
        )
        or 0
    )
    added = 0
    for name, instruction, category, processing in STANDARD_CAPTURE_STEPS:
        if name in existing_names:
            continue
        next_order += 1
        db.add(
            CaptureStep(
                dealership_id=dealership.id,
                name=name,
                instruction=instruction,
                category=category,
                capture_order=next_order,
                export_order=next_order,
                is_required=True,
                requires_processing=processing,
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
        f"/admin/dealerships/{dealership.id}/configuration#capture-steps",
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
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    dealership = _authorized_dealership(db, admin, dealership_id)
    if category not in {"exterior", "interior", "detail", "free"}:
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
    admin = _require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    _validate_csrf(request, csrf_token)
    step = db.get(CaptureStep, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="Fotoposition wurde nicht gefunden")
    dealership = _authorized_dealership(db, admin, step.dealership_id)
    if category not in {"exterior", "interior", "detail", "free"}:
        raise HTTPException(status_code=400, detail="Ungültige Kategorie")
    if not name.strip() or capture_order < 1 or (export_order is not None and export_order < 1):
        _flash(request, "Bitte prüfen Sie Name und Reihenfolge.", "error")
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
