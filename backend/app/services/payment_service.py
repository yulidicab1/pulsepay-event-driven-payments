from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.domain import (
    PaymentAlreadyExistsError,
    PaymentNotFoundError,
    build_payment_created_event,
    build_payment_metrics,
    build_processing_snapshot,
)
from app.core.logging import get_logger, log_event
from app.models.payment import Payment, PaymentEvent, PaymentStatus
from app.queue import QueuePublisher
from app.repositories import PaymentRepository
from app.schemas.payment import PaymentCreate
from app.schemas.processing import PaymentMetrics, PaymentProcessingSnapshot

logger = get_logger(__name__)


class PaymentService:
    def __init__(self, *, repository: PaymentRepository, publisher: QueuePublisher) -> None:
        self.repository = repository
        self.publisher = publisher

    def create_payment(self, payload: PaymentCreate) -> Payment:
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
            self.repository.commit()
        except IntegrityError as exc:
            self.repository.rollback()
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
        return self.repository.list()

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
