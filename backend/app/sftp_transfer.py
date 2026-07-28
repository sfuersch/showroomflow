from __future__ import annotations

import base64
import ftplib
import hashlib
import hmac
import io
import posixpath
import re
import socket
import ssl
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import paramiko
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import DealershipSftpSettings, ExportRun, JobStatus, VehicleJob
from app.storage import ObjectStorage


class SftpConfigurationError(RuntimeError):
    """The stored SFTP settings are missing or unsafe."""


class SftpTransferError(RuntimeError):
    """An archive could not be transferred."""


DEFAULT_SFTP_FILENAME_TEMPLATE = "<VIN>.zip"
VIN_FILENAME_TOKEN = "<VIN>"
TRANSFER_PROTOCOLS = {"sftp", "ftps"}


def normalize_protocol(value: str | None) -> str:
    protocol = (value or "sftp").strip().lower()
    if protocol not in TRANSFER_PROTOCOLS:
        raise SftpConfigurationError("Bitte wählen Sie SFTP oder FTPS als Übertragungsprotokoll.")
    return protocol


def _fernet(settings: Settings) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt_password(password: str, settings: Settings) -> str:
    if not password:
        raise SftpConfigurationError("Das Passwort darf nicht leer sein.")
    return _fernet(settings).encrypt(password.encode()).decode()


def decrypt_password(value: str, settings: Settings) -> str:
    try:
        return _fernet(settings).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise SftpConfigurationError("Das gespeicherte Passwort ist ungültig.") from exc


def normalize_remote_directory(value: str) -> str:
    cleaned = value.strip() or "/"
    if "\\" in cleaned or any(ord(character) < 32 for character in cleaned):
        raise SftpConfigurationError("Das SFTP-Zielverzeichnis ist ungültig.")
    parts = cleaned.split("/")
    if ".." in parts:
        raise SftpConfigurationError("Das SFTP-Zielverzeichnis darf kein '..' enthalten.")
    normalized = posixpath.normpath(cleaned)
    return normalized if normalized != "." else "/"


def normalize_filename_template(value: str | None) -> str:
    cleaned = (value or "").strip() or DEFAULT_SFTP_FILENAME_TEMPLATE
    if VIN_FILENAME_TOKEN not in cleaned:
        raise SftpConfigurationError(
            "Die SFTP-Dateinamensvorlage muss den Platzhalter <VIN> enthalten."
        )
    if (
        cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or any(ord(character) < 32 for character in cleaned)
    ):
        raise SftpConfigurationError("Die SFTP-Dateinamensvorlage ist ungültig.")
    if not cleaned.lower().endswith(".zip"):
        raise SftpConfigurationError(
            "Die SFTP-Dateinamensvorlage muss auf '.zip' enden."
        )
    if len(cleaned.replace(VIN_FILENAME_TOKEN, "W" * 17)) > 255:
        raise SftpConfigurationError("Der erzeugte SFTP-Dateiname ist zu lang.")
    return cleaned


def render_filename(template: str | None, vin: str) -> str:
    normalized = normalize_filename_template(template)
    safe_vin = re.sub(r"[^A-Za-z0-9_-]", "_", vin.strip()) or "FAHRZEUG"
    filename = normalized.replace(VIN_FILENAME_TOKEN, safe_vin)
    if len(filename) > 255:
        raise SftpConfigurationError("Der erzeugte SFTP-Dateiname ist zu lang.")
    return filename


def normalize_fingerprint(value: str) -> str:
    cleaned = value.strip().rstrip("=")
    if re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", cleaned) is None:
        raise SftpConfigurationError(
            "Der SFTP-Hostschlüssel muss als SHA256-Fingerabdruck angegeben werden."
        )
    return cleaned


def key_fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def fetch_host_key_fingerprint(host: str, port: int) -> str:
    cleaned_host = host.strip()
    if not cleaned_host:
        raise SftpConfigurationError("Bitte tragen Sie zuerst einen SFTP-Server ein.")
    if port < 1 or port > 65535:
        raise SftpConfigurationError("Der SFTP-Port ist ungültig.")
    sock: socket.socket | None = None
    transport: paramiko.Transport | None = None
    try:
        sock = socket.create_connection((cleaned_host, port), timeout=15)
        sock.settimeout(30)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=15)
        return key_fingerprint(transport.get_remote_server_key())
    except (OSError, paramiko.SSHException) as exc:
        raise SftpTransferError(f"SFTP-Hostschlüssel konnte nicht abgerufen werden: {exc}") from exc
    finally:
        if transport is not None:
            transport.close()
        elif sock is not None:
            sock.close()


def validate_settings(config: DealershipSftpSettings, runtime: Settings) -> str:
    protocol = normalize_protocol(config.protocol)
    if not config.host.strip() or not config.username.strip():
        raise SftpConfigurationError("Server und Benutzername sind erforderlich.")
    if config.port < 1 or config.port > 65535:
        raise SftpConfigurationError("Der Port ist ungültig.")
    if not config.password_encrypted:
        raise SftpConfigurationError("Es ist noch kein Passwort hinterlegt.")
    normalize_remote_directory(config.remote_directory)
    normalize_filename_template(config.filename_template)
    if protocol == "sftp":
        normalize_fingerprint(config.host_key_fingerprint)
    return decrypt_password(config.password_encrypted, runtime)


@contextmanager
def sftp_connection(
    config: DealershipSftpSettings, runtime: Settings
) -> Iterator[paramiko.SFTPClient]:
    password = validate_settings(config, runtime)
    sock: socket.socket | None = None
    transport: paramiko.Transport | None = None
    sftp: paramiko.SFTPClient | None = None
    try:
        sock = socket.create_connection((config.host.strip(), config.port), timeout=15)
        sock.settimeout(30)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=15)
        actual = key_fingerprint(transport.get_remote_server_key())
        expected = normalize_fingerprint(config.host_key_fingerprint)
        if not hmac.compare_digest(actual, expected):
            raise SftpConfigurationError(
                f"Der SFTP-Hostschlüssel stimmt nicht überein (empfangen: {actual})."
            )
        transport.auth_password(config.username.strip(), password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        if sftp is None:
            raise SftpTransferError("Die SFTP-Sitzung konnte nicht geöffnet werden.")
        yield sftp
    except (OSError, paramiko.SSHException) as exc:
        raise SftpTransferError(f"SFTP-Verbindung fehlgeschlagen: {exc}") from exc
    finally:
        if sftp is not None:
            sftp.close()
        if transport is not None:
            transport.close()
        elif sock is not None:
            sock.close()


def _ensure_remote_directory(sftp: paramiko.SFTPClient, directory: str) -> None:
    directory = normalize_remote_directory(directory)
    absolute = directory.startswith("/")
    current = "/" if absolute else ""
    for part in (part for part in directory.split("/") if part):
        current = posixpath.join(current, part)
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def test_sftp_connection(config: DealershipSftpSettings, runtime: Settings) -> None:
    with sftp_connection(config, runtime) as sftp:
        directory = normalize_remote_directory(config.remote_directory)
        _ensure_remote_directory(sftp, directory)
        sftp.stat(directory)


@contextmanager
def ftps_connection(
    config: DealershipSftpSettings, runtime: Settings
) -> Iterator[ftplib.FTP_TLS]:
    password = validate_settings(config, runtime)
    client = ftplib.FTP_TLS(
        context=ssl.create_default_context(),
        timeout=30,
    )
    try:
        client.connect(config.host.strip(), config.port, timeout=15)
        client.auth()
        client.login(config.username.strip(), password)
        client.prot_p()
        client.set_pasv(True)
        yield client
    except (OSError, ssl.SSLError, ftplib.Error) as exc:
        raise SftpTransferError(f"FTPS-Verbindung fehlgeschlagen: {exc}") from exc
    finally:
        try:
            client.quit()
        except (OSError, ssl.SSLError, ftplib.Error):
            client.close()


def _ensure_ftps_remote_directory(client: ftplib.FTP_TLS, directory: str) -> None:
    directory = normalize_remote_directory(directory)
    client.cwd("/")
    for part in (part for part in directory.split("/") if part):
        try:
            client.cwd(part)
        except ftplib.error_perm:
            client.mkd(part)
            client.cwd(part)


def test_transfer_connection(config: DealershipSftpSettings, runtime: Settings) -> None:
    if normalize_protocol(config.protocol) == "ftps":
        with ftps_connection(config, runtime) as client:
            _ensure_ftps_remote_directory(client, config.remote_directory)
            client.pwd()
        return
    test_sftp_connection(config, runtime)


def _validate_transfer_filename(filename: str) -> None:
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise SftpConfigurationError("Der Dateiname ist ungültig.")


def _upload_ftps_archive(
    config: DealershipSftpSettings,
    runtime: Settings,
    filename: str,
    content: bytes,
) -> str:
    _validate_transfer_filename(filename)
    directory = normalize_remote_directory(config.remote_directory)
    remote_path = posixpath.join(directory, filename)
    temporary_name = f"{filename}.{uuid.uuid4().hex}.part"
    with ftps_connection(config, runtime) as client:
        _ensure_ftps_remote_directory(client, directory)
        try:
            client.storbinary(f"STOR {temporary_name}", io.BytesIO(content))
            try:
                client.delete(filename)
            except ftplib.error_perm:
                pass
            client.rename(temporary_name, filename)
        except Exception:
            try:
                client.delete(temporary_name)
            except (OSError, ftplib.Error):
                pass
            raise
    return remote_path


def upload_archive(
    config: DealershipSftpSettings,
    runtime: Settings,
    filename: str,
    content: bytes,
) -> str:
    if normalize_protocol(config.protocol) == "ftps":
        return _upload_ftps_archive(config, runtime, filename, content)
    _validate_transfer_filename(filename)
    directory = normalize_remote_directory(config.remote_directory)
    remote_path = posixpath.join(directory, filename)
    temporary_path = f"{remote_path}.{uuid.uuid4().hex}.part"
    with sftp_connection(config, runtime) as sftp:
        _ensure_remote_directory(sftp, directory)
        try:
            with sftp.file(temporary_path, "wb") as target:
                target.write(content)
            try:
                sftp.posix_rename(temporary_path, remote_path)
            except (AttributeError, OSError):
                try:
                    sftp.remove(remote_path)
                except OSError:
                    pass
                sftp.rename(temporary_path, remote_path)
        except Exception:
            try:
                sftp.remove(temporary_path)
            except OSError:
                pass
            raise
    return remote_path


def transfer_export_run(export_run_id: str) -> None:
    identifier = uuid.UUID(export_run_id)
    runtime = get_settings()
    try:
        with SessionLocal() as db:
            export_run = db.get(ExportRun, identifier)
            if export_run is None:
                return
            job = db.get(VehicleJob, export_run.vehicle_job_id)
            if job is None or not export_run.object_key or export_run.status != "completed":
                raise SftpConfigurationError("Die ZIP-Datei ist noch nicht verfügbar.")
            config = db.get(DealershipSftpSettings, job.dealership_id)
            if config is None or not config.is_enabled:
                raise SftpConfigurationError("Die Dateiübertragung ist nicht aktiviert.")
            export_run.transfer_status = "processing"
            export_run.transfer_attempts += 1
            export_run.transfer_error = None
            db.commit()

            content = ObjectStorage(runtime).get_object(object_key=export_run.object_key)
            remote_filename = render_filename(config.filename_template, job.vin)
            remote_path = upload_archive(config, runtime, remote_filename, content)
            export_run.transfer_status = "completed"
            export_run.transferred_at = datetime.now(timezone.utc)
            export_run.remote_path = remote_path
            job.status = JobStatus.COMPLETED
            db.commit()
    except Exception as exc:
        with SessionLocal() as db:
            export_run = db.get(ExportRun, identifier)
            if export_run is not None:
                export_run.transfer_status = "failed"
                export_run.transfer_error = str(exc)[:1000]
                job = db.get(VehicleJob, export_run.vehicle_job_id)
                if job is not None:
                    job.status = JobStatus.REVIEW_REQUIRED
                db.commit()
        raise
