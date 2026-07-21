from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.dependencies import CurrentUser, DatabaseSession
from app.models import Dealership, RefreshSession, User, UserRole
from app.schemas import LoginRequest, RefreshRequest, TokenResponse, UserResponse
from app.security import (
    create_access_token,
    create_refresh_token,
    hash_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


def _issue_session(db: DatabaseSession, user: User) -> TokenResponse:
    access_token, expires_in = create_access_token(user.id)
    refresh_token, refresh_hash, refresh_expires_at = create_refresh_token()
    db.add(
        RefreshSession(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=refresh_expires_at,
        )
    )
    db.commit()
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: DatabaseSession) -> TokenResponse:
    email = str(payload.email).strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    dealership = db.get(Dealership, user.dealership_id) if user and user.dealership_id else None
    if (
        user is None
        or not user.is_active
        or user.role == UserRole.OPERATOR
        or (user.dealership_id is not None and (dealership is None or not dealership.is_active))
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="E-Mail-Adresse oder Passwort ist nicht korrekt",
        )
    return _issue_session(db, user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: DatabaseSession) -> TokenResponse:
    session = db.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == hash_refresh_token(payload.refresh_token)
        )
    )
    now = datetime.now(timezone.utc)
    expires_at = session.expires_at if session else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if session is None or session.revoked_at is not None or expires_at <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sitzung abgelaufen")

    user = db.get(User, session.user_id)
    if user is None or not user.is_active or user.role == UserRole.OPERATOR:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sitzung abgelaufen")

    session.revoked_at = now
    db.flush()
    return _issue_session(db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: RefreshRequest, db: DatabaseSession) -> None:
    session = db.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == hash_refresh_token(payload.refresh_token)
        )
    )
    if session is not None and session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        db.commit()


@router.get("/me", response_model=UserResponse)
def me(current_user: CurrentUser) -> User:
    return current_user
