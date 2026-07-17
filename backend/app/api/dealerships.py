from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.dependencies import DatabaseSession, SystemAdmin, UserAdmin
from app.models import Dealership, UserRole
from app.schemas import DealershipCreateRequest, DealershipResponse

router = APIRouter(prefix="/admin/dealerships", tags=["dealership administration"])


@router.get("", response_model=list[DealershipResponse])
def list_dealerships(db: DatabaseSession, admin: UserAdmin) -> list[Dealership]:
    statement = select(Dealership).order_by(Dealership.name)
    if admin.role == UserRole.DEALERSHIP_ADMIN:
        statement = statement.where(Dealership.id == admin.dealership_id)
    return list(db.scalars(statement))


@router.post("", response_model=DealershipResponse, status_code=status.HTTP_201_CREATED)
def create_dealership(
    payload: DealershipCreateRequest,
    db: DatabaseSession,
    _: SystemAdmin,
) -> Dealership:
    dealership = Dealership(
        name=payload.name.strip(),
        auto_export_enabled=payload.auto_export_enabled,
        retention_days=payload.retention_days,
        monthly_vehicle_credits=payload.monthly_vehicle_credits,
    )
    db.add(dealership)
    db.commit()
    db.refresh(dealership)
    return dealership
