import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.dependencies import CurrentUser, DatabaseSession, UserAdmin
from app.models import Dealership, Location, User, UserRole
from app.schemas import LocationCreateRequest, LocationResponse

router = APIRouter(prefix="/locations", tags=["locations"])


def _target_dealership(user: User, requested_id: uuid.UUID | None) -> uuid.UUID | None:
    if user.role != UserRole.SYSTEM_ADMIN:
        return user.dealership_id
    return requested_id


@router.get("", response_model=list[LocationResponse])
def list_locations(
    db: DatabaseSession,
    current_user: CurrentUser,
    dealership_id: uuid.UUID | None = Query(default=None),
) -> list[Location]:
    statement = select(Location).order_by(Location.name)
    target_id = _target_dealership(current_user, dealership_id)
    if current_user.role != UserRole.SYSTEM_ADMIN or target_id is not None:
        statement = statement.where(Location.dealership_id == target_id)
    return list(db.scalars(statement))


@router.post("", response_model=LocationResponse, status_code=status.HTTP_201_CREATED)
def create_location(
    payload: LocationCreateRequest,
    db: DatabaseSession,
    admin: UserAdmin,
) -> Location:
    dealership_id = _target_dealership(admin, payload.dealership_id)
    if dealership_id is None or db.get(Dealership, dealership_id) is None:
        raise HTTPException(status_code=422, detail="Autohaus wurde nicht gefunden")

    location = Location(dealership_id=dealership_id, name=payload.name.strip())
    db.add(location)
    db.commit()
    db.refresh(location)
    return location
