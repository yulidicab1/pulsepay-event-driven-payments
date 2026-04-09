from app.models.payment import Payment
from app.queue import QueueEnvelope


def build_payment_created_event(payment: Payment) -> QueueEnvelope:
    return QueueEnvelope(
        event_type="payment.created",
        payment_id=payment.id,
        payload={
            "payment_id": str(payment.id),
            "merchant_reference": payment.merchant_reference,
            "amount": str(payment.amount),
            "currency": payment.currency,
        },
    )

