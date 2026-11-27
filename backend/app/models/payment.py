import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.session import Base

JSONType = JSON().with_variant(JSONB, "postgresql")


PROVIDER_CONTRACT_SQL = """(
 (processing_backend IS NULL AND provider_account_id IS NULL AND provider_livemode IS NULL
  AND provider_idempotency_key IS NULL AND provider_request IS NULL AND provider_request_hash IS NULL
  AND provider_state IS NULL AND provider_status IS NULL AND provider_first_attempt_at IS NULL AND provider_retry_until IS NULL)
 OR
 (processing_backend = 'simulator' AND provider_reference IS NULL AND provider_account_id IS NULL AND provider_livemode IS NULL
  AND provider_idempotency_key IS NULL AND provider_request IS NULL AND provider_request_hash IS NULL
  AND provider_state IS NULL AND provider_status IS NULL AND provider_first_attempt_at IS NULL AND provider_retry_until IS NULL)
 OR
 (processing_backend = 'stripe' AND merchant_id IS NOT NULL AND provider_account_id IS NOT NULL
  AND provider_livemode IS FALSE AND provider_idempotency_key IS NOT NULL AND provider_request IS NOT NULL
  AND provider_request_hash IS NOT NULL AND length(provider_request_hash) = 64
  AND provider_state IN ('prepared','in_flight','pending','requires_action','unknown','review_required','succeeded','failed')
  AND (provider_status IS NULL OR provider_status IN ('succeeded','canceled','processing','requires_payment_method','requires_confirmation','requires_action','requires_capture'))
  AND ((provider_state = 'prepared' AND status = 'PENDING' AND provider_reference IS NULL AND provider_status IS NULL
        AND provider_first_attempt_at IS NULL AND provider_retry_until IS NULL)
       OR (provider_state <> 'prepared' AND provider_first_attempt_at IS NOT NULL
           AND provider_retry_until IS NOT NULL AND provider_retry_until > provider_first_attempt_at)))
) IS TRUE"""
STRIPE_REQUEST_SQL = """processing_backend IS DISTINCT FROM 'stripe' OR (
 provider_account_id ~ '^acct_[A-Za-z0-9]{1,250}$'
 AND (provider_reference IS NULL OR provider_reference ~ '^pi_[A-Za-z0-9_]{1,250}$')
 AND currency IN ('EUR','USD','GBP') AND amount > 0 AND amount <= 999999.99
 AND provider_idempotency_key = 'pp:stripe:v1:' || merchant_id::text || ':' || id::text
 AND provider_request_hash = encode(sha256(convert_to(provider_request, 'UTF8')), 'hex')
 AND (provider_request::jsonb->'params'->>'payment_method') ~ '^pm_[A-Za-z0-9_]{1,250}$'
 AND provider_request::jsonb = jsonb_build_object(
   'operation', 'payment_intent.create_confirm', 'api_version', '2026-06-24.dahlia',
   'account_id', provider_account_id, 'livemode', false,
   'params', jsonb_build_object(
     'amount', (amount * 100)::bigint, 'currency', lower(currency),
     'payment_method', provider_request::jsonb->'params'->>'payment_method',
     'confirm', true, 'capture_method', 'automatic',
     'automatic_payment_methods', jsonb_build_object('enabled', true, 'allow_redirects', 'never'),
     'metadata', jsonb_build_object('merchant_id', merchant_id::text, 'payment_id', id::text)))
 AND (provider_first_attempt_at IS NULL OR provider_retry_until = provider_first_attempt_at + interval '23 hours')
) IS TRUE"""

EXECUTION_CONTRACT_SQL = """(provider_attempt_count >= 0
 AND ((provider_execution_token IS NULL AND provider_lease_until IS NULL)
      OR (provider_execution_token IS NOT NULL AND provider_lease_until IS NOT NULL
          AND processing_backend = 'stripe' AND provider_state = 'in_flight'))
 AND (processing_backend = 'stripe' OR (provider_execution_token IS NULL
      AND provider_next_attempt_at IS NULL AND provider_attempt_count = 0))
 AND (processing_backend IS DISTINCT FROM 'stripe' OR (
      (provider_state = 'succeeded' AND status = 'COMPLETED' AND provider_status = 'succeeded' AND provider_reference IS NOT NULL)
   OR (provider_state = 'failed' AND status = 'FAILED' AND provider_status IN ('canceled','requires_payment_method') AND provider_reference IS NOT NULL)
   OR (provider_state = 'prepared' AND status = 'PENDING')
   OR (provider_state IN ('in_flight','pending','requires_action','unknown','review_required') AND status = 'PROCESSING')
 ))) IS TRUE"""


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
        UniqueConstraint("provider_account_id", "provider_livemode", "provider_idempotency_key", name="uq_payment_provider_key"),
        UniqueConstraint("provider_account_id", "provider_livemode", "provider_reference", name="uq_payment_provider_reference"),
        CheckConstraint(PROVIDER_CONTRACT_SQL, name="ck_payment_provider_contract"),
        CheckConstraint(STRIPE_REQUEST_SQL, name="ck_payment_stripe_request").ddl_if(dialect="postgresql"),
        CheckConstraint(EXECUTION_CONTRACT_SQL, name="ck_payment_execution_contract"),
        Index("ix_payment_provider_claim", "processing_backend", "provider_account_id", "provider_next_attempt_at"),
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
    # No server default: migrations preserve historical provider assignment as NULL.
    processing_backend: Mapped[str | None] = mapped_column(String(16), default="simulator", nullable=True)
    provider_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_livemode: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider_idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_retry_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_execution_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    provider_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_attempt_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0, nullable=False)
    provider_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
