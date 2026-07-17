from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import User, UserRole
from app.security import hash_password


def main() -> None:
    settings = get_settings()
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        print("Bootstrap admin configuration is not set; nothing to do.")
        return

    email = settings.bootstrap_admin_email.strip().lower()
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            print(f"System administrator {email} already exists.")
            return
        db.add(
            User(
                dealership_id=None,
                email=email,
                password_hash=hash_password(settings.bootstrap_admin_password),
                role=UserRole.SYSTEM_ADMIN,
            )
        )
        db.commit()
        print(f"System administrator {email} created.")


if __name__ == "__main__":
    main()
