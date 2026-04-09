from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.queue import PostgresQueueConsumer, PostgresQueuePublisher
from app.repositories import SqlAlchemyPaymentRepository
from app.services.payment_processor import PaymentProcessor
from app.services.payment_service import PaymentService
from app.services.worker_service import PaymentWorkerService


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


def build_worker_service(session: Session) -> PaymentWorkerService:
    repository = SqlAlchemyPaymentRepository(session)
    consumer = PostgresQueueConsumer(session)
    processor = PaymentProcessor()
    return PaymentWorkerService(
        payment_repository=repository,
        queue_consumer=consumer,
        processor=processor,
    )


def get_worker_service(session: Session = Depends(get_db_session)) -> PaymentWorkerService:
    return build_worker_service(session)
