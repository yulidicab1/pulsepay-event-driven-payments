from app.models.payment import Payment, PaymentEvent, PaymentStatus
from app.schemas.processing import PaymentMetrics, PaymentProcessingSnapshot


def build_payment_metrics(status_counts: dict[PaymentStatus, int]) -> PaymentMetrics:
    total = sum(status_counts.values())
    return PaymentMetrics(
        total=total,
        pending=status_counts.get(PaymentStatus.PENDING, 0),
        processing=status_counts.get(PaymentStatus.PROCESSING, 0),
        completed=status_counts.get(PaymentStatus.COMPLETED, 0),
        failed=status_counts.get(PaymentStatus.FAILED, 0),
    )


def build_processing_snapshot(
    payment: Payment,
    latest_event: PaymentEvent | None,
) -> PaymentProcessingSnapshot:
    return PaymentProcessingSnapshot(
        processing_backend=payment.processing_backend,
        provider_reference=payment.provider_reference,
        provider_state=payment.provider_state,
        provider_status=payment.provider_status,
        id=payment.id,
        merchant_reference=payment.merchant_reference,
        status=payment.status,
        retry_count=payment.retry_count,
        failure_reason=payment.failure_reason,
        latest_event_status=latest_event.status if latest_event else None,
        latest_event_attempts=latest_event.attempts if latest_event else None,
        latest_event_error=latest_event.last_error if latest_event else None,
    )

