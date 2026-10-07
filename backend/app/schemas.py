import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]+$"


def _normalize_email(v):
    # "" or whitespace -> None, otherwise trimmed + lowercased
    if isinstance(v, str):
        v = v.strip().lower()
        return v or None
    return v


# ---------- Responses ----------

class UserOut(BaseModel):
    """The ONLY shape a user is ever returned in. There is no password field
    here, so a hash can't leak even if a route returns the ORM object."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: str | None
    created_at: datetime
    updated_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user: UserOut


# ---------- Requests ----------

class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    email: EmailStr | None = None

    _norm_email = field_validator("email", mode="before")(_normalize_email)


class LoginIn(BaseModel):
    # Accept either field so clients can log in with username or email
    username: str | None = None
    email: str | None = None
    password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def need_identifier(self):
        if not (self.username or self.email):
            raise ValueError("username or email is required")
        return self

    @property
    def identifier(self) -> str:
        return (self.username or self.email).strip().lower()

# ---------- UserUpdate ----------
class UserUpdate(BaseModel):
    """PATCH body. Only these fields can change -- anything else sent
    (id, password_hash, created_at, ...) is ignored, so no mass assignment."""
    username: str | None = Field(default=None, min_length=3, max_length=32, pattern=USERNAME_PATTERN)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)  # new password
    current_password: str | None = Field(default=None, max_length=128)

    _norm_email = field_validator("email", mode="before")(_normalize_email)