from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings, get_settings


class StorageUnavailableError(RuntimeError):
    """Raised when the configured object store cannot be reached."""


class ObjectStorage:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.bucket = settings.storage_bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint,
            region_name=settings.storage_region,
            aws_access_key_id=settings.storage_access_key,
            aws_secret_access_key=settings.storage_secret_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def check_connection(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as exc:
            raise StorageUnavailableError("Object storage is unavailable") from exc

    def create_upload_url(
        self,
        *,
        object_key: str,
        content_type: str,
        expires_in: int = 900,
    ) -> str:
        if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise ValueError("Invalid object key")
        if expires_in < 1 or expires_in > 3600:
            raise ValueError("Upload URL expiry must be between 1 and 3600 seconds")
        return self._client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )

    def create_download_url(self, *, object_key: str, expires_in: int = 900) -> str:
        if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise ValueError("Invalid object key")
        if expires_in < 1 or expires_in > 3600:
            raise ValueError("Download URL expiry must be between 1 and 3600 seconds")
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": object_key},
            ExpiresIn=expires_in,
            HttpMethod="GET",
        )

    def put_object(self, *, object_key: str, content: bytes, content_type: str) -> None:
        if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise ValueError("Invalid object key")
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=content,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageUnavailableError("Object storage is unavailable") from exc


@lru_cache
def get_object_storage() -> ObjectStorage:
    return ObjectStorage(get_settings())
