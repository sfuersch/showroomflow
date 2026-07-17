from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.router import router
from app.config import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(
    title="ShowroomFlow API",
    version="0.1.0",
    description="API for guided vehicle photography, processing and SFTP exports.",
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_settings().allowed_hosts_list)
app.include_router(router, prefix="/api/v1")
