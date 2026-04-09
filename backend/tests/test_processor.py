from decimal import Decimal

from app.models.payment import Payment, PaymentStatus, ProcessingOutcome
from app.services.payment_processor import PaymentProcessor


def build_payment(merchant_reference: str, amount: str = "100.00") -> Payment:
    return Payment(
        merchant_reference=merchant_reference,
        amount=Decimal(amount),
        currency="USD",
        customer_email="demo@example.com",
        status=PaymentStatus.PENDING,
    )


def test_processor_completes_standard_payment():
    decision = PaymentProcessor(random_failure_rate=0).evaluate(
        build_payment("order-standard"),
        attempt_number=1,
    )

    assert decision.outcome == ProcessingOutcome.COMPLETED
    assert decision.reason is None


def test_processor_marks_high_value_payment_as_failed():
    decision = PaymentProcessor(random_failure_rate=0).evaluate(
        build_payment("order-enterprise", amount="5000.00"),
        attempt_number=1,
    )

    assert decision.outcome == ProcessingOutcome.FAILED
    assert decision.reason == "Deterministic payment rejection rule triggered"


def test_processor_retries_first_attempt_for_retry_marker():
    decision = PaymentProcessor(random_failure_rate=0).evaluate(
        build_payment("order-retry-simulation"),
        attempt_number=1,
    )

    assert decision.outcome == ProcessingOutcome.RETRY
    assert decision.reason == "Deterministic retry rule triggered on first attempt"


def test_processor_succeeds_on_second_retry_marker_attempt():
    decision = PaymentProcessor(random_failure_rate=0).evaluate(
        build_payment("order-retry-simulation"),
        attempt_number=2,
    )

    assert decision.outcome == ProcessingOutcome.COMPLETED
    assert decision.reason is None
