import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import UserOut, UserUpdate
from app.security import hash_password, verify_password

router = APIRouter(prefix="/api/users", tags=["users"])


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


def get_owned_user(user_id: str, current_user: User = Depends(get_current_user)) -> User:
    """Resolve :id to the caller's own account, or 404.

    Auth runs first (401 beats 404). Any id that isn't the caller's -- another
    user's, a nonexistent one, or not even a UUID -- gets the identical 404,
    and we never load another user's row at all.
    """
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        raise _not_found()
    if target_id != current_user.id:
        raise _not_found()
    return current_user


@router.get("/{user_id}", response_model=UserOut)
def read_user(user: User = Depends(get_owned_user)):
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    body: UserUpdate,
    user: User = Depends(get_owned_user),
    db: Session = Depends(get_db),
):
    fields = body.model_fields_set  # only what the client actually sent

    if "password" in fields and body.password is not None:
        # Changing the password requires proving you know the current one,
        # so a stolen token alone can't lock the real owner out.
        if not body.current_password or not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
        user.password_hash = hash_password(body.password)

    if "username" in fields and body.username is not None:
        user.username = body.username

    if "email" in fields:
        user.email = body.email  # null clears it

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username or email already taken")
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user: User = Depends(get_owned_user), db: Session = Depends(get_db)):
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)