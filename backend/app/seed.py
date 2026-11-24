"""Explicit, opt-in bootstrap for a development dashboard user."""

from app.core.config import read_secret, settings
from app.db.base import *  # noqa: F403
from app.db.session import SessionLocal
from app.repositories import SqlAlchemyMerchantRepository
from app.repositories.auth_repository import SqlAlchemyAuthRepository
from app.services.auth_service import AuthService
from app.services.merchant_service import MerchantService


def seed_dev_merchant() -> None:
    if settings.app_env != "development":
        raise RuntimeError("Development bootstrap is disabled outside development.")
    if not settings.seed_dev_enabled:
        return
    password = read_secret(None, settings.dev_user_password_file)
    if not settings.dev_user_email or not password:
        raise RuntimeError("Development user configuration is required.")
    with SessionLocal() as session:
        auth = AuthService(SqlAlchemyAuthRepository(session), settings)
        if auth.repository.get_user(settings.dev_user_email.lower()):
            return
        merchants = MerchantService(repository=SqlAlchemyMerchantRepository(session))
        merchant, _ = merchants.create_merchant(settings.dev_merchant_name, issue_api_key=False, commit=False)
        auth.create_user(merchant.id, settings.dev_user_email, password)


if __name__ == "__main__":
    seed_dev_merchant()
