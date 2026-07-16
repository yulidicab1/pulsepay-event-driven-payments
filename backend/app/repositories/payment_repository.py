from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.payment import IdempotencyKey, Payment, PaymentEvent, PaymentStatus


class PaymentRepository(ABC):
    @abstractmethod
    def add(self, payment: Payment) -> None:
        raise NotImplementedError

    @abstractmethod
    def flush(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def commit(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def rollback(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh(self, payment: Payment) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, payment_id: UUID) -> Payment | None:
        raise NotImplementedError

    @abstractmethod
    def list_payments(self) -> list[Payment]:
        raise NotImplementedError

    @abstractmethod
    def list_events(self, payment_id: UUID) -> list[PaymentEvent]:
        raise NotImplementedError

    @abstractmethod
    def get_latest_event(self, payment_id: UUID) -> PaymentEvent | None:
        raise NotImplementedError

    @abstractmethod
    def get_status_counts(self) -> dict[PaymentStatus, int]:
        raise NotImplementedError

    @abstractmethod
    def get_idempotency_key(self, key: str) -> IdempotencyKey | None:
        raise NotImplementedError

    @abstractmethod
    def add_idempotency_key(self, record: IdempotencyKey) -> None:
        raise NotImplementedError


class SqlAlchemyPaymentRepository(PaymentRepository):
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, payment: Payment) -> None:
        self.session.add(payment)

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def refresh(self, payment: Payment) -> None:
        self.session.refresh(payment)

    def get(self, payment_id: UUID) -> Payment | None:
        return self.session.get(Payment, payment_id)

    def list_payments(self) -> list[Payment]:
        statement = select(Payment).order_by(Payment.created_at.desc())
        return list(self.session.scalars(statement))

    def list_events(self, payment_id: UUID) -> list[PaymentEvent]:
        statement = (
            select(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id)
            .order_by(PaymentEvent.created_at.desc())
        )
        return list(self.session.scalars(statement))

    def get_latest_event(self, payment_id: UUID) -> PaymentEvent | None:
        statement = (
            select(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id)
            .order_by(PaymentEvent.created_at.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def get_status_counts(self) -> dict[PaymentStatus, int]:
        rows = self.session.execute(
            select(Payment.status, func.count(Payment.id)).group_by(Payment.status)
        ).all()
        return dict(rows)

    def get_idempotency_key(self, key: str) -> IdempotencyKey | None:
        statement = select(IdempotencyKey).where(IdempotencyKey.key == key)
        return self.session.scalar(statement)

    def add_idempotency_key(self, record: IdempotencyKey) -> None:
        self.session.add(record)
