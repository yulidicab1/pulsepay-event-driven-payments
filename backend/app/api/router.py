from fastapi import APIRouter

from app.api.routes import health, payments
from app.core.config import settings

api_router = APIRouter(prefix=settings.api_v1_prefix)
api_router.include_router(health.router, tags=["health"])
api_router.include_router(payments.router, prefix="/payments", tags=["payments"])

