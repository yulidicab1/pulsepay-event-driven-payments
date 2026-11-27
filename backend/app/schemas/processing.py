from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.payment import PaymentStatus, QueueMessageStatus
from app.schemas.provider import PaymentProviderInfo, public_processing_note


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

    _public_error = field_validator("last_error", mode="before")(public_processing_note)


class PaymentProcessingSnapshot(PaymentProviderInfo):
    id: UUID
    merchant_reference: str
    status: PaymentStatus
    retry_count: int
    failure_reason: str | None
    latest_event_status: QueueMessageStatus | None
    latest_event_attempts: int | None
    latest_event_error: str | None

    _public_notes = field_validator("failure_reason", "latest_event_error", mode="before")(public_processing_note)
