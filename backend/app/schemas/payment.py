from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.payment import PaymentStatus
from app.schemas.provider import PaymentProviderInfo, public_processing_note


class PaymentCreate(BaseModel):
    merchant_reference: str = Field(min_length=3, max_length=64)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    customer_email: EmailStr
    description: str | None = Field(default=None, max_length=500)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class PaymentBaseResponse(PaymentProviderInfo):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    merchant_reference: str
    amount: Decimal
    currency: str
    customer_email: EmailStr
    description: str | None
    status: PaymentStatus
    failure_reason: str | None
    retry_count: int
    created_at: datetime
    updated_at: datetime

    _public_reason = field_validator("failure_reason", mode="before")(public_processing_note)


class PaymentDetail(PaymentBaseResponse):
    pass


class PaymentListItem(PaymentBaseResponse):
    pass


class PaymentStatusUpdate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: PaymentStatus
    retry_count: int
    failure_reason: str | None
    updated_at: datetime

    _public_reason = field_validator("failure_reason", mode="before")(public_processing_note)
