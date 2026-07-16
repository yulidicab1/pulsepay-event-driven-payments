from uuid import UUID


class PaymentAlreadyExistsError(Exception):
    def __init__(self, merchant_reference: str) -> None:
        super().__init__(f"Payment with merchant reference {merchant_reference} already exists")
        self.merchant_reference = merchant_reference


class PaymentNotFoundError(Exception):
    def __init__(self, payment_id: UUID) -> None:
        super().__init__(f"Payment {payment_id} not found")
        self.payment_id = payment_id


class IdempotencyConflictError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(
            f"Idempotency-Key {key} was already used with a different request payload"
        )
        self.key = key

