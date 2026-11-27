from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.logging import get_logger, log_event
from app.models.payment import PaymentStatus, ProcessingOutcome
from app.queue import QueueConsumer
from app.repositories import PaymentRepository
from app.services.payment_processor import PaymentProcessor
from app.services.stripe_processor import StripePaymentProcessor, StripeProcessingDecision

logger = get_logger(__name__)


@dataclass(frozen=True)
class WorkerBatchResult:
    claimed: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0
    processing: int = 0
    unknown: int = 0
    review_required: int = 0
    stale: int = 0


class PaymentWorkerService:
    def __init__(
        self,
        *,
        payment_repository: PaymentRepository,
        queue_consumer: QueueConsumer,
        processor: PaymentProcessor | None = None,
        stripe_processor: StripePaymentProcessor | None = None,
        stripe_account_id: str | None = None,
        stripe_lease_seconds: int = 120,
        stripe_retry_delay_seconds: int = 30,
    ) -> None:
        self.payment_repository = payment_repository
        self.queue_consumer = queue_consumer
        self.processor = processor or PaymentProcessor()
        if stripe_processor is not None and stripe_account_id is None:
            raise ValueError("Stripe processing requires an explicit account.")
        self.stripe_processor = stripe_processor
        self.stripe_account_id = stripe_account_id
        self.stripe_lease_seconds = stripe_lease_seconds
        self.stripe_retry_delay_seconds = stripe_retry_delay_seconds

    def process_available_events(self, *, batch_size: int | None = None) -> WorkerBatchResult:
        limit = batch_size or settings.worker_batch_size
        counts = asdict(self._process_simulator_events(batch_size=limit))
        if self.stripe_processor is None:
            return WorkerBatchResult(**counts)
        # Claim one operation immediately before each call: queued batch members
        # cannot lose their leases while another provider request is in progress.
        for _ in range(limit):
            operation = self.payment_repository.claim_provider_operation(
                account_id=self.stripe_account_id, lease_seconds=self.stripe_lease_seconds)
            if operation is None:
                break
            counts["claimed"] += 1
            decision = self.stripe_processor.evaluate(operation)
            try:
                applied = self.payment_repository.finish_provider_operation(operation,
                    state=decision.state, provider_status=decision.provider_status,
                    provider_reference=decision.provider_reference, reason=decision.reason,
                    retry_delay_seconds=self.stripe_retry_delay_seconds)
            except IntegrityError as exc:
                if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "uq_payment_provider_reference":
                    raise
                # The result transaction rolled back. Keep the original contract
                # and stop automatic emissions instead of retrying a conflicting ID.
                decision = StripeProcessingDecision("review_required", reason="provider_reference_conflict")
                applied = self.payment_repository.finish_provider_operation(operation,
                    state=decision.state, reason=decision.reason)
            if not applied:
                counts["stale"] += 1
            else:
                counter = {"succeeded": "completed", "failed": "failed", "unknown": "unknown",
                           "review_required": "review_required"}.get(decision.state, "processing")
                counts[counter] += 1
            log_event(logger, "stripe_operation_result", payment_id=operation.payment_id,
                      applied=applied, state=decision.state)
        return WorkerBatchResult(**counts)

    def _process_simulator_events(self, *, batch_size: int | None = None) -> WorkerBatchResult:
        claimed_messages = self.queue_consumer.claim_available(batch_size=batch_size or settings.worker_batch_size)
        result = WorkerBatchResult(claimed=len(claimed_messages))

        if not claimed_messages:
            self.payment_repository.commit()
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
