import io
from contextlib import contextmanager

import paramiko
import pytest

from app.config import Settings
from app.models import DealershipSftpSettings
from app.sftp_transfer import (
    SftpConfigurationError,
    decrypt_password,
    encrypt_password,
    key_fingerprint,
    normalize_fingerprint,
    normalize_remote_directory,
    upload_archive,
    validate_settings,
)


def test_sftp_password_is_encrypted_and_can_be_decrypted() -> None:
    runtime = Settings(secret_key="s" * 64)
    encrypted = encrypt_password("very-secret-password", runtime)

    assert encrypted != "very-secret-password"
    assert decrypt_password(encrypted, runtime) == "very-secret-password"


def test_remote_directory_rejects_parent_traversal() -> None:
    with pytest.raises(SftpConfigurationError):
        normalize_remote_directory("/incoming/../private")


def test_sha256_fingerprint_is_normalized_and_matches_paramiko_key() -> None:
    fingerprint = key_fingerprint(paramiko.RSAKey.generate(1024))

    assert fingerprint.startswith("SHA256:")
    assert normalize_fingerprint(f"{fingerprint}===") == fingerprint


def test_sftp_configuration_requires_password_and_fingerprint() -> None:
    config = DealershipSftpSettings(
        host="sftp.example.de",
        port=22,
        username="showroomflow",
        remote_directory="/incoming",
    )
    with pytest.raises(SftpConfigurationError):
        validate_settings(config, Settings(secret_key="s" * 64))


def test_upload_archive_uses_temporary_file_and_atomic_rename(monkeypatch) -> None:
    runtime = Settings(secret_key="s" * 64)
    config = DealershipSftpSettings(
        host="sftp.example.de",
        port=22,
        username="showroomflow",
        password_encrypted=encrypt_password("secret", runtime),
        remote_directory="/",
        host_key_fingerprint="SHA256:" + "A" * 43,
    )

    class FakeSftp:
        def __init__(self) -> None:
            self.files: dict[str, io.BytesIO] = {}
            self.renamed: tuple[str, str] | None = None

        def file(self, path: str, mode: str) -> io.BytesIO:
            assert mode == "wb"
            target = io.BytesIO()
            self.files[path] = target
            return target

        def posix_rename(self, source: str, destination: str) -> None:
            self.renamed = (source, destination)

        def remove(self, path: str) -> None:
            self.files.pop(path, None)

    fake = FakeSftp()

    @contextmanager
    def fake_connection(_config, _runtime):
        yield fake

    monkeypatch.setattr("app.sftp_transfer.sftp_connection", fake_connection)
    remote_path = upload_archive(config, runtime, "VIN123.zip", b"zip-content")

    assert remote_path == "/VIN123.zip"
    assert fake.renamed is not None
    assert fake.renamed[0].endswith(".part")
    assert fake.renamed[1] == remote_path
