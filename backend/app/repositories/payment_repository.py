from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
import re
import time
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.payment import IdempotencyKey, Payment, PaymentEvent, PaymentStatus, QueueMessageStatus


@dataclass(frozen=True)
class ProviderOperation:
    payment_id: UUID
    merchant_id: UUID
    account_id: str
    execution_token: UUID = field(repr=False)
    lease_until: datetime
    claimed_at: datetime
    claimed_monotonic: float = field(repr=False)
    amount: Decimal
    currency: str
    idempotency_key: str = field(repr=False)
    request: str = field(repr=False)
    request_hash: str = field(repr=False)
    provider_reference: str | None
    retry_until: datetime
    attempt_count: int


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
    def claim_provider_operation(self, *, account_id: str, lease_seconds: int,
                                 merchant_id: UUID | None = None) -> ProviderOperation | None:
        raise NotImplementedError

    @abstractmethod
    def finish_provider_operation(self, operation: ProviderOperation, *, state: str,
                                  provider_status: str | None = None, provider_reference: str | None = None,
                                  reason: str | None = None, retry_delay_seconds: int = 30) -> bool:
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

    def claim_provider_operation(self, *, account_id: str, lease_seconds: int,
                                 merchant_id: UUID | None = None) -> ProviderOperation | None:
        if self.session.in_transaction():
            raise RuntimeError("Provider claims require an idle transaction.")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid provider lease duration.")
        started = time.monotonic()
        with self.session.begin():
            now = self.session.scalar(select(func.clock_timestamp()))
            # A late duplicate of a final operation is acknowledged without I/O.
            for states, event_status in [(["succeeded"], QueueMessageStatus.COMPLETED),
                                          (["failed", "review_required"], QueueMessageStatus.FAILED)]:
                terminal = select(Payment.id).where(Payment.id == PaymentEvent.payment_id,
                    Payment.processing_backend == "stripe", Payment.provider_account_id == account_id,
                    Payment.provider_livemode.is_(False), Payment.provider_state.in_(states))
                if merchant_id is not None:
                    terminal = terminal.where(Payment.merchant_id == merchant_id)
                self.session.execute(update(PaymentEvent).where(
                    PaymentEvent.queue_name == settings.payment_queue_name,
                    PaymentEvent.status.in_([QueueMessageStatus.PENDING, QueueMessageStatus.PROCESSING]),
                    terminal.exists(),
                ).values(status=event_status))
            event_available = select(PaymentEvent.id).where(
                PaymentEvent.payment_id == Payment.id,
                PaymentEvent.queue_name == settings.payment_queue_name,
                PaymentEvent.status.in_([QueueMessageStatus.PENDING, QueueMessageStatus.PROCESSING]),
                PaymentEvent.available_at <= now,
            ).exists()
            statement = select(Payment).where(
                Payment.processing_backend == "stripe", Payment.merchant_id.is_not(None),
                Payment.provider_account_id == account_id, Payment.provider_livemode.is_(False),
                Payment.provider_state.not_in(["succeeded", "failed", "review_required"]),
                or_(Payment.provider_lease_until.is_(None), Payment.provider_lease_until <= now),
                or_(Payment.provider_next_attempt_at.is_(None), Payment.provider_next_attempt_at <= now),
                event_available,
            )
            if merchant_id is not None:
                statement = statement.where(Payment.merchant_id == merchant_id)
            payment = self.session.scalar(statement.order_by(Payment.created_at, Payment.id).limit(1)
                .with_for_update(skip_locked=True, of=Payment).execution_options(populate_existing=True))
            if payment is None:
                return None
            # Refresh DB time after the lock; the committed token fences every later write.
            now = self.session.scalar(select(func.clock_timestamp()))
            payment.provider_execution_token = uuid4()
            payment.provider_lease_until = now + timedelta(seconds=lease_seconds)
            payment.provider_attempt_count += 1
            if payment.provider_first_attempt_at is None:
                payment.provider_first_attempt_at = now
                payment.provider_retry_until = now + timedelta(hours=23)
            payment.provider_state = "in_flight"
            payment.status = PaymentStatus.PROCESSING
            self.session.execute(update(PaymentEvent).where(
                PaymentEvent.payment_id == payment.id,
                PaymentEvent.queue_name == settings.payment_queue_name,
                PaymentEvent.status.in_([QueueMessageStatus.PENDING, QueueMessageStatus.PROCESSING]),
            ).values(status=QueueMessageStatus.PROCESSING, attempts=PaymentEvent.attempts + 1))
            self.session.flush()
            return ProviderOperation(payment.id, payment.merchant_id, payment.provider_account_id,
                payment.provider_execution_token, payment.provider_lease_until, now, started,
                payment.amount, payment.currency, payment.provider_idempotency_key, payment.provider_request,
                payment.provider_request_hash, payment.provider_reference, payment.provider_retry_until,
                payment.provider_attempt_count)

    def finish_provider_operation(self, operation: ProviderOperation, *, state: str,
                                  provider_status: str | None = None, provider_reference: str | None = None,
                                  reason: str | None = None, retry_delay_seconds: int = 30) -> bool:
        if self.session.in_transaction():
            raise RuntimeError("Provider results require an idle transaction.")
        if state not in {"succeeded", "failed", "pending", "requires_action", "unknown", "review_required"}:
            raise ValueError("Invalid provider outcome.")
        if reason is not None and re.fullmatch(r"[a-z_]{1,64}", reason) is None:
            raise ValueError("Provider reason must be a fixed safe code.")
        if not 1 <= retry_delay_seconds <= 3600:
            raise ValueError("Invalid provider retry delay.")
        with self.session.begin():
            payment = self.session.scalar(select(Payment).where(
                Payment.id == operation.payment_id, Payment.merchant_id == operation.merchant_id,
                Payment.processing_backend == "stripe", Payment.provider_account_id == operation.account_id,
                Payment.provider_livemode.is_(False), Payment.provider_execution_token == operation.execution_token,
            ).with_for_update().execution_options(populate_existing=True))
            now = self.session.scalar(select(func.clock_timestamp()))
            if payment is None or payment.provider_lease_until <= now:
                return False
            payment.provider_state = state
            payment.status = {"succeeded": PaymentStatus.COMPLETED, "failed": PaymentStatus.FAILED}.get(state, PaymentStatus.PROCESSING)
            payment.failure_reason = reason
            if provider_reference is not None:
                payment.provider_reference = provider_reference
            if provider_status is not None:
                payment.provider_status = provider_status
            payment.provider_execution_token = None
            payment.provider_lease_until = None
            terminal = state in {"succeeded", "failed", "review_required"}
            payment.provider_next_attempt_at = None if terminal else now + timedelta(seconds=retry_delay_seconds)
            payment.retry_count = payment.provider_attempt_count
            event_status = (QueueMessageStatus.COMPLETED if state == "succeeded" else
                            QueueMessageStatus.FAILED if terminal else QueueMessageStatus.PENDING)
            self.session.execute(update(PaymentEvent).where(
                PaymentEvent.payment_id == payment.id,
                PaymentEvent.queue_name == settings.payment_queue_name,
                PaymentEvent.status.in_([QueueMessageStatus.PROCESSING, QueueMessageStatus.PENDING]),
            ).values(status=event_status, last_error=reason,
                     available_at=payment.provider_next_attempt_at or now))
            self.session.flush()
            return True

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
