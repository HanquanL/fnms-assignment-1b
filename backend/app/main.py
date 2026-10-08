from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app import models  # noqa: F401  (registers the User table on Base.metadata)
from app.db import Base, engine
from app.routers import auth, users, tracker



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables if they don't exist. Idempotent: safe on every startup,
    # never drops or alters existing data.
    Base.metadata.create_all(bind=engine)
    yield

app = FastAPI(title="FNMS A1 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,  # explicit list, never "*"
    allow_credentials=False,                  # we use Bearer tokens, not cookies
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # FastAPI's default 422 body echoes the submitted value back ("input"),
    # which would reflect passwords. Keep only where and why it failed.
    errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}

app.include_router(auth.router)
app.include_router(tracker.router)
app.include_router(users.router)