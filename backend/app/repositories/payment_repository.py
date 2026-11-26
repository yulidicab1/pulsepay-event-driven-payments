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
    def get(self, payment_id: UUID, merchant_id: UUID | None = None) -> Payment | None:
        raise NotImplementedError

    @abstractmethod
    def get_provider_operation(self, payment_id: UUID, merchant_id: UUID, *, account_id: str,
                               for_update: bool = False) -> Payment | None:
        raise NotImplementedError

    @abstractmethod
    def list_payments(self, merchant_id: UUID) -> list[Payment]:
        raise NotImplementedError

    @abstractmethod
    def list_events(self, payment_id: UUID, merchant_id: UUID) -> list[PaymentEvent]:
        raise NotImplementedError

    @abstractmethod
    def get_latest_event(self, payment_id: UUID, merchant_id: UUID) -> PaymentEvent | None:
        raise NotImplementedError

    @abstractmethod
    def get_status_counts(self, merchant_id: UUID) -> dict[PaymentStatus, int]:
        raise NotImplementedError

    @abstractmethod
    def get_idempotency_key(self, merchant_id: UUID, key: str) -> IdempotencyKey | None:
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

    def get(self, payment_id: UUID, merchant_id: UUID | None = None) -> Payment | None:
        statement = select(Payment).where(Payment.id == payment_id)
        if merchant_id is not None:
            statement = statement.where(Payment.merchant_id == merchant_id)
        return self.session.scalar(statement)

    def get_provider_operation(self, payment_id: UUID, merchant_id: UUID, *, account_id: str,
                               for_update: bool = False) -> Payment | None:
        statement = select(Payment).where(
            Payment.id == payment_id, Payment.merchant_id == merchant_id,
            Payment.processing_backend == "stripe", Payment.provider_account_id == account_id,
            Payment.provider_livemode.is_(False),
        ).execution_options(populate_existing=True)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_payments(self, merchant_id: UUID) -> list[Payment]:
        statement = (
            select(Payment)
            .where(Payment.merchant_id == merchant_id)
            .order_by(Payment.created_at.desc())
        )
        return list(self.session.scalars(statement))

    def list_events(self, payment_id: UUID, merchant_id: UUID) -> list[PaymentEvent]:
        statement = (
            select(PaymentEvent)
            .join(Payment, Payment.id == PaymentEvent.payment_id)
            .where(Payment.merchant_id == merchant_id)
            .where(PaymentEvent.payment_id == payment_id)
            .order_by(PaymentEvent.created_at.desc())
        )
        return list(self.session.scalars(statement))

    def get_latest_event(self, payment_id: UUID, merchant_id: UUID) -> PaymentEvent | None:
        statement = (
            select(PaymentEvent)
            .join(Payment, Payment.id == PaymentEvent.payment_id)
            .where(Payment.merchant_id == merchant_id)
            .where(PaymentEvent.payment_id == payment_id)
            .order_by(PaymentEvent.created_at.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def get_status_counts(self, merchant_id: UUID) -> dict[PaymentStatus, int]:
        rows = self.session.execute(
            select(Payment.status, func.count(Payment.id))
            .where(Payment.merchant_id == merchant_id)
            .group_by(Payment.status)
        ).all()
        return dict(rows)

    def get_idempotency_key(self, merchant_id: UUID, key: str) -> IdempotencyKey | None:
        statement = (
            select(IdempotencyKey)
            .where(IdempotencyKey.merchant_id == merchant_id)
            .where(IdempotencyKey.key == key)
        )
        return self.session.scalar(statement)

    def add_idempotency_key(self, record: IdempotencyKey) -> None:
        self.session.add(record)
