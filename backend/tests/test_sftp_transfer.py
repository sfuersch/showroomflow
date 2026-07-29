import ftplib
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
    fetch_host_key_fingerprint,
    fetch_tls_certificate_fingerprint,
    ftps_connection,
    key_fingerprint,
    normalize_filename_template,
    normalize_fingerprint,
    normalize_protocol,
    normalize_remote_directory,
    normalize_tls_certificate_fingerprint,
    render_filename,
    tls_certificate_fingerprint,
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


def test_transfer_protocol_accepts_sftp_and_ftps() -> None:
    assert normalize_protocol("sftp") == "sftp"
    assert normalize_protocol("FTPS") == "ftps"
    with pytest.raises(SftpConfigurationError):
        normalize_protocol("ftp")


def test_sftp_filename_template_replaces_vin() -> None:
    template = normalize_filename_template("225067-ZHZ-img_<VIN>.zip")

    assert render_filename(template, "WF0/TEST 123") == "225067-ZHZ-img_WF0_TEST_123.zip"


@pytest.mark.parametrize(
    "template",
    [
        "225067-ZHZ-img.zip",
        "../<VIN>.zip",
        "archive/<VIN>.zip",
        "<VIN>.jpg",
    ],
)
def test_sftp_filename_template_rejects_unsafe_values(template: str) -> None:
    with pytest.raises(SftpConfigurationError):
        normalize_filename_template(template)


def test_sha256_fingerprint_is_normalized_and_matches_paramiko_key() -> None:
    fingerprint = key_fingerprint(paramiko.RSAKey.generate(1024))

    assert fingerprint.startswith("SHA256:")
    assert normalize_fingerprint(f"{fingerprint}===") == fingerprint


def test_host_fingerprint_can_be_fetched_without_credentials(monkeypatch) -> None:
    key = paramiko.RSAKey.generate(1024)

    class FakeSocket:
        def settimeout(self, _timeout: int) -> None:
            pass

    class FakeTransport:
        def __init__(self, _socket) -> None:
            pass

        def start_client(self, timeout: int) -> None:
            assert timeout == 15

        def get_remote_server_key(self):
            return key

        def close(self) -> None:
            pass

    monkeypatch.setattr("app.sftp_transfer.socket.create_connection", lambda *_args, **_kw: FakeSocket())
    monkeypatch.setattr("app.sftp_transfer.paramiko.Transport", FakeTransport)

    assert fetch_host_key_fingerprint("sftp.example.de", 22) == key_fingerprint(key)


def test_sftp_configuration_requires_password_and_fingerprint() -> None:
    config = DealershipSftpSettings(
        host="sftp.example.de",
        port=22,
        username="showroomflow",
        remote_directory="/incoming",
    )
    with pytest.raises(SftpConfigurationError):
        validate_settings(config, Settings(secret_key="s" * 64))


def test_ftps_configuration_does_not_require_ssh_fingerprint() -> None:
    runtime = Settings(secret_key="s" * 64)
    config = DealershipSftpSettings(
        protocol="ftps",
        host="ftp.example.de",
        port=21,
        username="showroomflow",
        password_encrypted=encrypt_password("secret", runtime),
        remote_directory="/incoming",
        host_key_fingerprint="",
    )

    assert validate_settings(config, runtime) == "secret"


def test_tls_certificate_fingerprint_is_normalized() -> None:
    raw = "ab" * 32

    assert normalize_tls_certificate_fingerprint(raw) == (
        "SHA256:" + ":".join(["AB"] * 32)
    )
    assert normalize_tls_certificate_fingerprint(
        "sha256:" + ":".join(["ab"] * 32)
    ) == ("SHA256:" + ":".join(["AB"] * 32))
    assert tls_certificate_fingerprint(b"certificate") == (
        normalize_tls_certificate_fingerprint(
            "03d66dd08835c1ca3f128cceacd1f31ac94163096b20f445ae84285bc0832d72"
        )
    )

    with pytest.raises(SftpConfigurationError):
        normalize_tls_certificate_fingerprint("not-a-fingerprint")


def test_ftps_certificate_can_be_fetched_without_ca_trust(monkeypatch) -> None:
    certificate = b"self-signed-certificate"

    class FakeTlsSocket:
        def getpeercert(self, *, binary_form: bool):
            assert binary_form is True
            return certificate

    class FakeFtps:
        def __init__(self, *, context, timeout) -> None:
            assert context.check_hostname is False
            assert context.verify_mode == 0
            assert timeout == 30
            self.sock = FakeTlsSocket()

        def connect(self, host: str, port: int, *, timeout: int) -> None:
            assert (host, port, timeout) == ("ftp.example.de", 21, 15)

        def auth(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("app.sftp_transfer.ftplib.FTP_TLS", FakeFtps)

    assert fetch_tls_certificate_fingerprint(
        "ftp.example.de", 21
    ) == tls_certificate_fingerprint(certificate)


def test_ftps_connection_enables_tls_for_control_and_data_channels(monkeypatch) -> None:
    runtime = Settings(secret_key="s" * 64)
    config = DealershipSftpSettings(
        protocol="ftps",
        host="ftp.example.de",
        port=21,
        username="showroomflow",
        password_encrypted=encrypt_password("secret", runtime),
        remote_directory="/",
    )
    calls: list[tuple] = []

    class FakeFtps:
        def __init__(self, *, context, timeout) -> None:
            calls.append(("init", context.check_hostname, context.verify_mode, timeout))

        def connect(self, host: str, port: int, *, timeout: int) -> None:
            calls.append(("connect", host, port, timeout))

        def auth(self) -> None:
            calls.append(("auth",))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def prot_p(self) -> None:
            calls.append(("prot_p",))

        def set_pasv(self, enabled: bool) -> None:
            calls.append(("set_pasv", enabled))

        def quit(self) -> None:
            calls.append(("quit",))

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr("app.sftp_transfer.ftplib.FTP_TLS", FakeFtps)

    with ftps_connection(config, runtime):
        calls.append(("yield",))

    assert calls == [
        ("init", True, 2, 30),
        ("connect", "ftp.example.de", 21, 15),
        ("auth",),
        ("login", "showroomflow", "secret"),
        ("prot_p",),
        ("set_pasv", True),
        ("yield",),
        ("quit",),
    ]


def test_ftps_connection_accepts_only_the_pinned_self_signed_certificate(
    monkeypatch,
) -> None:
    runtime = Settings(secret_key="s" * 64)
    certificate = b"self-signed-certificate"
    config = DealershipSftpSettings(
        protocol="ftps",
        host="ftp.example.de",
        port=21,
        username="showroomflow",
        password_encrypted=encrypt_password("secret", runtime),
        remote_directory="/",
        tls_certificate_fingerprint=tls_certificate_fingerprint(certificate),
    )
    calls: list[tuple] = []

    class FakeTlsSocket:
        def getpeercert(self, *, binary_form: bool):
            assert binary_form is True
            calls.append(("certificate",))
            return certificate

    class FakeFtps:
        def __init__(self, *, context, timeout) -> None:
            calls.append(("init", context.check_hostname, context.verify_mode, timeout))
            self.sock = FakeTlsSocket()

        def connect(self, host: str, port: int, *, timeout: int) -> None:
            calls.append(("connect", host, port, timeout))

        def auth(self) -> None:
            calls.append(("auth",))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def prot_p(self) -> None:
            calls.append(("prot_p",))

        def set_pasv(self, enabled: bool) -> None:
            calls.append(("set_pasv", enabled))

        def quit(self) -> None:
            calls.append(("quit",))

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr("app.sftp_transfer.ftplib.FTP_TLS", FakeFtps)

    with ftps_connection(config, runtime):
        calls.append(("yield",))

    assert calls.index(("certificate",)) < calls.index(
        ("login", "showroomflow", "secret")
    )
    assert ("init", False, 0, 30) in calls


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


def test_ftps_upload_uses_private_data_channel_and_atomic_rename(monkeypatch) -> None:
    runtime = Settings(secret_key="s" * 64)
    config = DealershipSftpSettings(
        protocol="ftps",
        host="ftp.example.de",
        port=21,
        username="showroomflow",
        password_encrypted=encrypt_password("secret", runtime),
        remote_directory="/incoming",
        filename_template="<VIN>.zip",
    )

    class FakeFtps:
        def __init__(self) -> None:
            self.cwd_calls: list[str] = []
            self.stored: tuple[str, bytes] | None = None
            self.renamed: tuple[str, str] | None = None

        def cwd(self, directory: str) -> None:
            self.cwd_calls.append(directory)

        def mkd(self, _directory: str) -> None:
            raise AssertionError("Existing directory must not be created")

        def storbinary(self, command: str, stream: io.BytesIO) -> None:
            self.stored = (command, stream.read())

        def delete(self, _filename: str) -> None:
            raise ftplib.error_perm("550 file not found")

        def rename(self, source: str, destination: str) -> None:
            self.renamed = (source, destination)

    fake = FakeFtps()

    @contextmanager
    def fake_connection(_config, _runtime):
        yield fake

    monkeypatch.setattr("app.sftp_transfer.ftps_connection", fake_connection)
    remote_path = upload_archive(config, runtime, "VIN123.zip", b"zip-content")

    assert remote_path == "/incoming/VIN123.zip"
    assert fake.cwd_calls == ["/", "incoming"]
    assert fake.stored is not None
    assert fake.stored[0].startswith("STOR VIN123.zip.")
    assert fake.stored[0].endswith(".part")
    assert fake.stored[1] == b"zip-content"
    assert fake.renamed is not None
    assert fake.renamed[0].endswith(".part")
    assert fake.renamed[1] == "VIN123.zip"
