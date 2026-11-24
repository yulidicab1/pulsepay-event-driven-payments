from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, log_event
from app.db.base import *  # noqa: F403
from app.services.auth_service import AuthError


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The database schema is managed by Alembic migrations (`alembic upgrade
    # head`), run before the app starts. See docker-compose.yml and the README.
    configure_logging()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "X-CSRF-Token", "Idempotency-Key", "X-Admin-Token"],
)
app.include_router(api_router)


@app.exception_handler(AuthError)
async def auth_error(_: Request, exc: AuthError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code, headers={"Cache-Control": "no-store"})


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError):
    errors = [{"loc": item["loc"], "msg": "Invalid value", "type": item["type"]} for item in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(SQLAlchemyError)
async def database_error(_: Request, exc: SQLAlchemyError):
    log_event(get_logger(__name__), "database_request_failed", error_type=type(exc).__name__)
    return JSONResponse(
        {"detail": "The operation could not be completed."},
        status_code=503,
        headers={"Cache-Control": "no-store"},
    )


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(settings.api_v1_prefix):
        response.headers["Cache-Control"] = "no-store"
    return response
