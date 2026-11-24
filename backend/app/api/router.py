from fastapi import APIRouter

from app.api.routes import auth, health, merchants, payments
from app.core.config import settings

api_router = APIRouter(prefix=settings.api_v1_prefix)
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(merchants.router, prefix="/merchants", tags=["merchants"])
api_router.include_router(payments.router, prefix="/payments", tags=["payments"])

