from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.payment import PaymentStatus, QueueMessageStatus


class PaymentMetrics(BaseModel):
    total: int
    pending: int
    processing: int
    completed: int
    failed: int


class PaymentEventItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    payment_id: UUID
    queue_name: str
    event_type: str
    status: QueueMessageStatus
    attempts: int
    last_error: str | None
    available_at: datetime
    created_at: datetime
    updated_at: datetime


class PaymentProcessingSnapshot(BaseModel):
    id: UUID
    merchant_reference: str
    status: PaymentStatus
    retry_count: int
    failure_reason: str | None
    latest_event_status: QueueMessageStatus | None
    latest_event_attempts: int | None
    latest_event_error: str | None
