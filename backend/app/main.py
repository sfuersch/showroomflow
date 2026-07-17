from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.api.router import router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(
    title="ShowroomFlow API",
    version="0.1.0",
    description="API for guided vehicle photography, processing and SFTP exports.",
    lifespan=lifespan,
)
app.include_router(router, prefix="/api/v1")
