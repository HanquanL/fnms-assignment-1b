from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import decode_access_token

# auto_error=False: raise our own 401. With auto_error=True some FastAPI
bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    # No header / wrong scheme
    if creds is None or creds.scheme.lower() != "bearer":
        raise _unauthorized()

    # Bad signature, malformed, expired, missing claims
    user_id = decode_access_token(creds.credentials)
    if user_id is None:
        raise _unauthorized()

    # Valid token but the account has since been deleted
    user = db.get(User, user_id)
    if user is None:
        raise _unauthorized()

    return user