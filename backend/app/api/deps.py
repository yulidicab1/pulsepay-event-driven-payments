from collections.abc import Generator

import secrets

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import read_secret, settings
from app.db.session import SessionLocal
from app.models.payment import Merchant
from app.queue import PostgresQueueConsumer, PostgresQueuePublisher
from app.repositories import SqlAlchemyMerchantRepository, SqlAlchemyPaymentRepository
from app.repositories.auth_repository import SqlAlchemyAuthRepository
from app.services.auth_service import AuthError, AuthService
from app.services.merchant_service import MerchantService
from app.services.payment_processor import PaymentProcessor
from app.services.payment_service import PaymentService
from app.services.worker_service import PaymentWorkerService
from app.services.stripe_gateway import StripeGateway
from app.services.stripe_processor import StripePaymentProcessor


def get_db_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def build_payment_service(session: Session) -> PaymentService:
    repository = SqlAlchemyPaymentRepository(session)
    publisher = PostgresQueuePublisher(session)
    return PaymentService(repository=repository, publisher=publisher)


def get_payment_service(session: Session = Depends(get_db_session)) -> PaymentService:
    return build_payment_service(session)


def get_merchant_service(session: Session = Depends(get_db_session)) -> MerchantService:
    return MerchantService(repository=SqlAlchemyMerchantRepository(session))


def get_auth_service(session: Session = Depends(get_db_session)) -> AuthService:
    return AuthService(SqlAlchemyAuthRepository(session), settings)


def require_origin(request: Request) -> None:
    if request.headers.get("origin") != settings.public_origin:
        raise AuthError("Request origin is not allowed.", 403)


def get_current_merchant(
    request: Request,
    authorization: str | None = Header(default=None),
    merchant_service: MerchantService = Depends(get_merchant_service),
    auth_service: AuthService = Depends(get_auth_service),
) -> Merchant:
    cookie = request.cookies.get(settings.session_cookie_name)
    if authorization is not None:
        if cookie or len(authorization) > 256 or not authorization.lower().startswith("bearer "):
            raise AuthError()
        merchant = merchant_service.authenticate(authorization.split(" ", 1)[1].strip())
        if merchant is None:
            raise AuthError()
        return merchant
    if not cookie:
        raise AuthError()
    unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
    if unsafe:
        require_origin(request)
    user = auth_service.authenticate(cookie, request.headers.get("X-CSRF-Token"), unsafe=unsafe)
    return user.merchant


def require_admin_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    expected = read_secret(settings.admin_api_token, settings.admin_api_token_file)
    if not expected or not x_admin_token or not secrets.compare_digest(expected.encode(), x_admin_token.encode()):
        raise AuthError("Administrative access denied.", 403)


def build_worker_service(session: Session) -> PaymentWorkerService:
    repository = SqlAlchemyPaymentRepository(session)
    consumer = PostgresQueueConsumer(session)
    processor = PaymentProcessor()
    stripe_processor = None
    if settings.stripe_worker_enabled:
        stripe_processor = StripePaymentProcessor(gateway=StripeGateway(
            secret_key=settings.get_stripe_secret_key(), timeout_seconds=settings.stripe_timeout_seconds,
            account_id=settings.stripe_account_id), request_budget_seconds=4 * settings.stripe_timeout_seconds + 1)
    return PaymentWorkerService(
        payment_repository=repository,
        queue_consumer=consumer,
        processor=processor,
        stripe_processor=stripe_processor,
        stripe_account_id=settings.stripe_account_id,
        stripe_lease_seconds=settings.stripe_lease_seconds,
        stripe_retry_delay_seconds=settings.stripe_reconcile_delay_seconds,
    )


def get_worker_service(session: Session = Depends(get_db_session)) -> PaymentWorkerService:
    return build_worker_service(session)
