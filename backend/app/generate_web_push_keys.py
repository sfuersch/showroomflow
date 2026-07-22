"""Generate one VAPID key pair for ShowroomFlow Web Push."""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def main() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_der = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_point = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    print(f"SHOWROOMFLOW_WEB_PUSH_VAPID_PUBLIC_KEY={base64url(public_point)}")
    print(f"SHOWROOMFLOW_WEB_PUSH_VAPID_PRIVATE_KEY={base64url(private_der)}")
    print("SHOWROOMFLOW_WEB_PUSH_VAPID_SUBJECT=https://showroomflow.promotekk.com")


if __name__ == "__main__":
    main()
