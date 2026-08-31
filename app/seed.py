"""Create three fake demo users. Run with: python -m app.seed"""

import os

from sqlalchemy import select

from app.auth import password_hash
from app.database import Base, SessionLocal, engine
from app.models import User


DEMO_USERS = (
    ("alice@example.com", "Alice Demo"),
    ("bob@example.com", "Bob Demo"),
    ("carol@example.com", "Carol Demo"),
)


def main() -> None:
    password = os.getenv("DEMO_USER_PASSWORD")
    if not password or len(password) < 8:
        raise SystemExit("Set DEMO_USER_PASSWORD to a fake password of at least 8 characters.")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        created = 0
        for email, display_name in DEMO_USERS:
            if db.scalar(select(User).where(User.email == email)) is None:
                db.add(User(email=email, display_name=display_name, password_hash=password_hash.hash(password)))
                created += 1
        db.commit()
    print(f"Created {created} demo user(s); {len(DEMO_USERS) - created} already existed.")


if __name__ == "__main__":
    main()
