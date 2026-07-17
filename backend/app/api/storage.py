from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import SystemAdmin
from app.schemas import StorageHealthResponse
from app.storage import ObjectStorage, StorageUnavailableError, get_object_storage

router = APIRouter(prefix="/admin/storage", tags=["storage"])
StorageDependency = Annotated[ObjectStorage, Depends(get_object_storage)]


@router.get("/health", response_model=StorageHealthResponse)
def storage_health(_: SystemAdmin, storage: StorageDependency) -> StorageHealthResponse:
    try:
        storage.check_connection()
    except StorageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bildspeicher ist nicht erreichbar",
        ) from exc
    return StorageHealthResponse(
        status="ok",
        provider="cloudflare-r2",
        bucket=storage.bucket,
    )
