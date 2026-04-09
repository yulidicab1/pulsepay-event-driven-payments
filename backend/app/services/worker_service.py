from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.logging import get_logger, log_event
from app.models.payment import PaymentStatus, ProcessingOutcome
from app.queue import QueueConsumer
from app.repositories import PaymentRepository
from app.services.payment_processor import PaymentProcessor

logger = get_logger(__name__)


@dataclass(frozen=True)
class WorkerBatchResult:
    claimed: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0


class PaymentWorkerService:
    def __init__(
        self,
        *,
        payment_repository: PaymentRepository,
        queue_consumer: QueueConsumer,
        processor: PaymentProcessor | None = None,
    ) -> None:
        self.payment_repository = payment_repository
        self.queue_consumer = queue_consumer
        self.processor = processor or PaymentProcessor()

    def process_available_events(self, *, batch_size: int | None = None) -> WorkerBatchResult:
        claimed_messages = self.queue_consumer.claim_available(batch_size=batch_size or settings.worker_batch_size)
        result = WorkerBatchResult(claimed=len(claimed_messages))

        if not claimed_messages:
            return result

        completed = 0
        retried = 0
        failed = 0

        for message in claimed_messages:
            payment = self.payment_repository.get(message.payment_id)
            if payment is None:
                self.queue_consumer.mark_failed(message.id, last_error="Payment record no longer exists")
                failed += 1
                log_event(
                    logger,
                    "queue_message_failed_missing_payment",
                    message_id=message.id,
                    payment_id=message.payment_id,
                )
                continue

            payment.status = PaymentStatus.PROCESSING
            payment.failure_reason = None
            self.payment_repository.flush()

            decision = self.processor.evaluate(payment, attempt_number=message.attempts)
            if decision.outcome == ProcessingOutcome.COMPLETED:
                payment.status = PaymentStatus.COMPLETED
                payment.failure_reason = None
                self.queue_consumer.mark_completed(message.id)
                completed += 1
                log_event(
                    logger,
                    "payment_processing_completed",
                    payment_id=payment.id,
                    message_id=message.id,
                    attempts=message.attempts,
                )
                continue

            payment.retry_count = message.attempts
            payment.failure_reason = decision.reason

            if decision.outcome == ProcessingOutcome.RETRY and message.attempts < settings.payment_max_retries:
                payment.status = PaymentStatus.PENDING
                self.queue_consumer.requeue(
                    message.id,
                    available_at=datetime.now(UTC) + timedelta(seconds=settings.payment_retry_delay_seconds),
                    last_error=decision.reason or "Retry scheduled",
                )
                retried += 1
                log_event(
                    logger,
                    "payment_processing_retry_scheduled",
                    payment_id=payment.id,
                    message_id=message.id,
                    attempts=message.attempts,
                    retry_count=payment.retry_count,
                    reason=decision.reason,
                )
                continue

            payment.status = PaymentStatus.FAILED
            self.queue_consumer.mark_failed(message.id, last_error=decision.reason or "Payment processing failed")
            failed += 1
            log_event(
                logger,
                "payment_processing_failed",
                payment_id=payment.id,
                message_id=message.id,
                attempts=message.attempts,
                retry_count=payment.retry_count,
                reason=decision.reason,
            )

        self.payment_repository.commit()
        return WorkerBatchResult(
            claimed=len(claimed_messages),
            completed=completed,
            retried=retried,
            failed=failed,
        )
