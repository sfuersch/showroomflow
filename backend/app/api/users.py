import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DatabaseSession, UserAdmin
from app.models import Dealership, RefreshSession, User, UserRole
from app.schemas import UserCreateRequest, UserResponse, UserUpdateRequest
from app.security import hash_password

router = APIRouter(prefix="/admin/users", tags=["user administration"])


def _target_dealership(admin: User, requested_id: uuid.UUID | None) -> uuid.UUID | None:
    if admin.role == UserRole.DEALERSHIP_ADMIN:
        return admin.dealership_id
    return requested_id


@router.get("", response_model=list[UserResponse])
def list_users(
    db: DatabaseSession,
    admin: UserAdmin,
    dealership_id: uuid.UUID | None = Query(default=None),
) -> list[User]:
    statement = select(User).order_by(User.email)
    target_id = _target_dealership(admin, dealership_id)
    if admin.role == UserRole.DEALERSHIP_ADMIN or target_id is not None:
        statement = statement.where(User.dealership_id == target_id)
    return list(db.scalars(statement))


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreateRequest, db: DatabaseSession, admin: UserAdmin) -> User:
    if admin.role == UserRole.DEALERSHIP_ADMIN and payload.role == UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Keine Berechtigung")

    dealership_id = _target_dealership(admin, payload.dealership_id)
    if payload.role != UserRole.SYSTEM_ADMIN and dealership_id is None:
        raise HTTPException(status_code=422, detail="Autohaus ist erforderlich")
    if payload.role == UserRole.SYSTEM_ADMIN:
        dealership_id = None
    elif db.get(Dealership, dealership_id) is None:
        raise HTTPException(status_code=422, detail="Autohaus wurde nicht gefunden")

    user = User(
        dealership_id=dealership_id,
        email=str(payload.email).strip().lower(),
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="E-Mail-Adresse ist bereits vorhanden"
        ) from None
    db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    db: DatabaseSession,
    admin: UserAdmin,
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Benutzer wurde nicht gefunden")
    if admin.role == UserRole.DEALERSHIP_ADMIN and user.dealership_id != admin.dealership_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Keine Berechtigung")
    if user.id == admin.id and payload.is_active is False:
        raise HTTPException(
            status_code=422, detail="Das eigene Konto kann nicht deaktiviert werden"
        )

    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
    if payload.password is not None or payload.is_active is False:
        db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
    db.commit()
    db.refresh(user)
    return user
