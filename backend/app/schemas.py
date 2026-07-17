from datetime import datetime

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    retention_days: int
    timestamp: datetime


class AppInfoResponse(BaseModel):
    name: str
    version: str
    minimum_ios_version: str
    output_width: int
    output_height: int
