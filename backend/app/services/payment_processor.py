import random
from dataclasses import dataclass
from decimal import Decimal

from app.core.config import settings
from app.models.payment import Payment, ProcessingOutcome


@dataclass(frozen=True)
class ProcessingDecision:
    outcome: ProcessingOutcome
    reason: str | None = None


class PaymentProcessor:
    def __init__(self, random_failure_rate: float | None = None) -> None:
        self.random_failure_rate = (
            settings.payment_random_failure_rate if random_failure_rate is None else random_failure_rate
        )

    def evaluate(self, payment: Payment, *, attempt_number: int) -> ProcessingDecision:
        merchant_reference = payment.merchant_reference.lower()

        if "fail" in merchant_reference or payment.amount >= Decimal("5000.00"):
            return ProcessingDecision(
                outcome=ProcessingOutcome.FAILED,
                reason="Deterministic payment rejection rule triggered",
            )

        if "retry" in merchant_reference and attempt_number == 1:
            return ProcessingDecision(
                outcome=ProcessingOutcome.RETRY,
                reason="Deterministic retry rule triggered on first attempt",
            )

        if self.random_failure_rate > 0 and random.random() < self.random_failure_rate:
            return ProcessingDecision(
                outcome=ProcessingOutcome.RETRY,
                reason="Random transient processing failure triggered",
            )

        return ProcessingDecision(outcome=ProcessingOutcome.COMPLETED)
