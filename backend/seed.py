"""Create (or repair) the grading account. Safe to run any number of times.

    python seed.py          # create NYUgrader if it doesn't exist
    python seed.py --reset  # also reset its password back to the assignment value
"""
import argparse

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import models  # noqa: F401  (registers the User table on Base.metadata)
from app.db import Base, SessionLocal, engine
from app.models import User
from app.security import hash_password, verify_password

# Given in the assignment handout -- public by design, not a secret.
GRADER_USERNAME = "NYUgrader"
GRADER_PASSWORD = "Courant2026!"
GRADER_EMAIL = "nyugrader@example.com"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="reset the grader password if it was changed")
    args = parser.parse_args()

    # Make sure the table exists even if the server has never been started
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.username) == GRADER_USERNAME.lower()))

        if user is None:
            db.add(User(
                username=GRADER_USERNAME,
                email=GRADER_EMAIL,
                password_hash=hash_password(GRADER_PASSWORD),
            ))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise SystemExit(f"Could not create {GRADER_USERNAME}: email {GRADER_EMAIL} is already in use.")
            print(f"Created {GRADER_USERNAME}.")
            return

        if verify_password(GRADER_PASSWORD, user.password_hash):
            print(f"{GRADER_USERNAME} already exists and the password is correct. Nothing to do.")
            return

        if args.reset:
            user.password_hash = hash_password(GRADER_PASSWORD)
            db.commit()
            print(f"{GRADER_USERNAME} existed with a different password; password reset.")
        else:
            print(f"{GRADER_USERNAME} exists but its password was changed. Run: python seed.py --reset")


if __name__ == "__main__":
    main()