import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, ForeignKeyConstraint, Index, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.session import Base

JSONType = JSON().with_variant(JSONB, "postgresql")


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    api_keys: Mapped[list["MerchantApiKey"]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )


class MerchantApiKey(Base):
    __tablename__ = "merchant_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    # Short, non-secret prefix shown in dashboards to identify a key.
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    # SHA-256 hash of the full secret key. The plaintext is never stored.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    merchant: Mapped["Merchant"] = relationship(back_populates="api_keys")


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class QueueMessageStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("merchant_id", "merchant_reference", name="uq_payments_merchant_reference"),
        UniqueConstraint("id", "merchant_id", name="uq_payment_owner"),
        Index("uq_legacy_payment_reference", "merchant_reference", unique=True, postgresql_where=text("merchant_id IS NULL"), sqlite_where=text("merchant_id IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    merchant_reference: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    customer_email: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"),
        default=PaymentStatus.PENDING,
        index=True,
    )
    failure_reason: Mapped[str] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    events: Mapped[list["PaymentEvent"]] = relationship(back_populates="payment", cascade="all, delete-orphan")


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"), index=True)
    queue_name: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    status: Mapped[QueueMessageStatus] = mapped_column(
        Enum(QueueMessageStatus, name="queue_message_status"),
        default=QueueMessageStatus.PENDING,
        index=True,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    payment: Mapped["Payment"] = relationship(back_populates="events")


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("merchant_id", "key", name="uq_idempotency_keys_merchant_key"),
        ForeignKeyConstraint(["payment_id", "merchant_id"], ["payments.id", "payments.merchant_id"], name="fk_idempotency_payment_owner", deferrable=True, initially="DEFERRED"),
        Index("uq_legacy_idempotency_key", "key", unique=True, postgresql_where=text("merchant_id IS NULL"), sqlite_where=text("merchant_id IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    key: Mapped[str] = mapped_column(String(255), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ProcessingOutcome(str, enum.Enum):
    COMPLETED = "COMPLETED"
    RETRY = "RETRY"
    FAILED = "FAILED"
