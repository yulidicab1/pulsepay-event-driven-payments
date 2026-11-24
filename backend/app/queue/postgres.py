from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.payment import Payment, PaymentEvent, QueueMessageStatus
from app.queue.base import ClaimedMessage, QueueConsumer, QueueEnvelope, QueuePublisher


class PostgresQueuePublisher(QueuePublisher):
    def __init__(self, session: Session) -> None:
        self.session = session

    def publish(self, envelope: QueueEnvelope) -> None:
        message = PaymentEvent(
            payment_id=envelope.payment_id,
            queue_name=settings.payment_queue_name,
            event_type=envelope.event_type,
            payload=envelope.payload,
        )
        self.session.add(message)


class PostgresQueueConsumer(QueueConsumer):
    def __init__(self, session: Session) -> None:
        self.session = session

    def claim_available(self, *, batch_size: int) -> list[ClaimedMessage]:
        statement = (
            select(PaymentEvent)
            .join(Payment, Payment.id == PaymentEvent.payment_id)
            .where(Payment.merchant_id.is_not(None))
            .where(PaymentEvent.status == QueueMessageStatus.PENDING)
            .where(PaymentEvent.queue_name == settings.payment_queue_name)
            .where(PaymentEvent.available_at <= datetime.now(UTC))
            .order_by(PaymentEvent.created_at.asc())
            .limit(batch_size)
            # Lock the claimed rows so concurrent workers never grab the same
            # message. skip_locked lets other workers move on to other rows
            # instead of blocking. On PostgreSQL this renders FOR UPDATE SKIP
            # LOCKED; SQLite (used in tests) silently ignores it.
            .with_for_update(skip_locked=True, of=PaymentEvent)
        )
        events = list(self.session.scalars(statement))
        for event in events:
            event.status = QueueMessageStatus.PROCESSING
            event.attempts += 1
        self.session.flush()
        return [
            ClaimedMessage(
                id=event.id,
                payment_id=event.payment_id,
                event_type=event.event_type,
                payload=event.payload,
                status=event.status,
                attempts=event.attempts,
                available_at=event.available_at,
                created_at=event.created_at,
            )
            for event in events
        ]

    def mark_completed(self, message_id: UUID) -> None:
        event = self._get_message(message_id)
        event.status = QueueMessageStatus.COMPLETED
        event.last_error = None
        self.session.flush()

    def requeue(self, message_id: UUID, *, available_at: datetime, last_error: str) -> None:
        event = self._get_message(message_id)
        event.status = QueueMessageStatus.PENDING
        event.available_at = available_at
        event.last_error = last_error
        self.session.flush()

    def mark_failed(self, message_id: UUID, *, last_error: str) -> None:
        event = self._get_message(message_id)
        event.status = QueueMessageStatus.FAILED
        event.last_error = last_error
        self.session.flush()

    def _get_message(self, message_id: UUID) -> PaymentEvent:
        message = self.session.get(PaymentEvent, message_id)
        if message is None:
            raise ValueError(f"Queue message {message_id} was not found")
        return message
