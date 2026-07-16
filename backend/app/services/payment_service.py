import hashlib
import json
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.domain import (
    IdempotencyConflictError,
    PaymentAlreadyExistsError,
    PaymentNotFoundError,
    build_payment_created_event,
    build_payment_metrics,
    build_processing_snapshot,
)
from app.core.logging import get_logger, log_event
from app.models.payment import IdempotencyKey, Payment, PaymentEvent, PaymentStatus
from app.queue import QueuePublisher
from app.repositories import PaymentRepository
from app.schemas.payment import PaymentCreate
from app.schemas.processing import PaymentMetrics, PaymentProcessingSnapshot

logger = get_logger(__name__)


class PaymentService:
    def __init__(self, *, repository: PaymentRepository, publisher: QueuePublisher) -> None:
        self.repository = repository
        self.publisher = publisher

    @staticmethod
    def _hash_payload(payload: PaymentCreate) -> str:
        canonical = json.dumps(
            {
                "merchant_reference": payload.merchant_reference,
                "amount": str(payload.amount),
                "currency": payload.currency,
                "customer_email": str(payload.customer_email),
                "description": payload.description,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def create_payment(self, payload: PaymentCreate, *, idempotency_key: str | None = None) -> Payment:
        request_hash = self._hash_payload(payload) if idempotency_key is not None else None

        if idempotency_key is not None:
            existing_key = self.repository.get_idempotency_key(idempotency_key)
            if existing_key is not None:
                if existing_key.request_hash != request_hash:
                    raise IdempotencyConflictError(idempotency_key)
                replayed = self.repository.get(existing_key.payment_id)
                if replayed is not None:
                    return replayed

        payment = Payment(
            merchant_reference=payload.merchant_reference,
            amount=payload.amount,
            currency=payload.currency,
            customer_email=payload.customer_email,
            description=payload.description,
            status=PaymentStatus.PENDING,
        )
        self.repository.add(payment)

        try:
            self.repository.flush()
            self.publisher.publish(build_payment_created_event(payment))
            if idempotency_key is not None:
                self.repository.add_idempotency_key(
                    IdempotencyKey(
                        key=idempotency_key,
                        request_hash=request_hash,
                        payment_id=payment.id,
                    )
                )
            self.repository.commit()
        except IntegrityError as exc:
            self.repository.rollback()
            # A concurrent request may have inserted the same idempotency key
            # between our lookup and commit. Treat that as a replay instead of a
            # duplicate-merchant conflict.
            if idempotency_key is not None:
                existing_key = self.repository.get_idempotency_key(idempotency_key)
                if existing_key is not None and existing_key.request_hash == request_hash:
                    replayed = self.repository.get(existing_key.payment_id)
                    if replayed is not None:
                        return replayed
            raise PaymentAlreadyExistsError(payload.merchant_reference) from exc

        self.repository.refresh(payment)
        log_event(
            logger,
            "payment_created",
            payment_id=payment.id,
            merchant_reference=payment.merchant_reference,
            amount=payment.amount,
            currency=payment.currency,
            status=payment.status.value,
        )
        return payment

    def list_payments(self) -> list[Payment]:
        return self.repository.list_payments()

    def get_payment(self, payment_id: UUID) -> Payment | None:
        return self.repository.get(payment_id)

    def require_payment(self, payment_id: UUID) -> Payment:
        payment = self.repository.get(payment_id)
        if payment is None:
            raise PaymentNotFoundError(payment_id)
        return payment

    def get_payment_events(self, payment_id: UUID) -> list[PaymentEvent]:
        self.require_payment(payment_id)
        return self.repository.list_events(payment_id)

    def get_metrics(self) -> PaymentMetrics:
        return build_payment_metrics(self.repository.get_status_counts())

    def get_processing_snapshot(self, payment_id: UUID) -> PaymentProcessingSnapshot | None:
        payment = self.repository.get(payment_id)
        if payment is None:
            return None

        latest_event = self.repository.get_latest_event(payment_id)
        return build_processing_snapshot(payment, latest_event)
