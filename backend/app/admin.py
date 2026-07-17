import secrets
import uuid
from datetime import datetime, timezone
from hmac import compare_digest
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Dealership, Location, RefreshSession, User, UserRole
from app.security import hash_password, verify_password

router = APIRouter(prefix="/admin", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


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
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        _context(request, admin, dealerships=dealerships),
    )


@router.get("/dealerships", response_class=HTMLResponse)
def dealerships_page(request: Request, db: Session = Depends(get_db)):
    return dashboard(request, db)


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
    dealership = Dealership(name=cleaned_name, retention_days=90, auto_export_enabled=False)
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
