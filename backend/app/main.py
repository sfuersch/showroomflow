from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.admin import router as admin_router
from app.api.router import router
from app.config import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


settings = get_settings()
app = FastAPI(
    title="ShowroomFlow API",
    version="0.1.0",
    description="API for guided vehicle photography, processing and SFTP exports.",
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="showroomflow_admin_session",
    max_age=settings.admin_session_hours * 3600,
    same_site="lax",
    https_only=settings.environment == "production",
)
app.mount(
    "/admin/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="admin-static",
)
app.include_router(admin_router)
app.include_router(router, prefix="/api/v1")
