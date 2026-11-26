import hashlib
import json
import re
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from app.domain import (
    IdempotencyConflictError,
    PaymentAlreadyExistsError,
    PaymentNotFoundError,
    build_payment_created_event,
    build_payment_metrics,
    build_processing_snapshot,
)
from app.core.logging import get_logger, log_event
from app.models.payment import IdempotencyKey, Payment, PaymentEvent, PaymentStatus
from app.queue import QueuePublisher
from app.repositories import PaymentRepository
from app.schemas.payment import PaymentCreate
from app.schemas.processing import PaymentMetrics, PaymentProcessingSnapshot
from app.services.stripe_gateway import STRIPE_API_VERSION, to_minor_units

logger = get_logger(__name__)


class PaymentService:
    def __init__(self, *, repository: PaymentRepository, publisher: QueuePublisher) -> None:
        self.repository = repository
        self.publisher = publisher

    @staticmethod
    def _hash_payload(payload: PaymentCreate) -> str:
        canonical = json.dumps(
            {
                "merchant_reference": payload.merchant_reference,
                "amount": str(payload.amount),
                "currency": payload.currency,
                "customer_email": str(payload.customer_email),
                "description": payload.description,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _prepare_processing(payment: Payment, backend: str, account_id: str | None,
                            payment_method: str | None) -> None:
        if backend not in {"simulator", "stripe"}:
            raise ValueError("Unsupported payment provider.")
        payment.processing_backend = backend
        if backend == "simulator":
            if account_id is not None or payment_method is not None:
                raise ValueError("Simulator payments cannot carry a provider account.")
            return
        if (
            not isinstance(account_id, str) or re.fullmatch(r"acct_[A-Za-z0-9]{1,250}", account_id) is None
            or not isinstance(payment_method, str) or re.fullmatch(r"pm_[A-Za-z0-9_]{1,250}", payment_method) is None
        ):
            raise ValueError("Invalid test provider context.")
        params = {
            "amount": to_minor_units(payment.amount, payment.currency), "currency": payment.currency.lower(),
            "payment_method": payment_method, "confirm": True, "capture_method": "automatic",
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
            "metadata": {"merchant_id": str(payment.merchant_id), "payment_id": str(payment.id)},
        }
        snapshot = json.dumps({
            "operation": "payment_intent.create_confirm", "api_version": STRIPE_API_VERSION,
            "account_id": account_id, "livemode": False, "params": params,
        }, sort_keys=True, separators=(",", ":"))
        payment.provider_account_id = account_id
        payment.provider_livemode = False
        payment.provider_idempotency_key = f"pp:stripe:v1:{payment.merchant_id}:{payment.id}"
        payment.provider_request = snapshot
        payment.provider_request_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        payment.provider_state = "prepared"

    def create_payment(
        self,
        payload: PaymentCreate,
        *,
        merchant_id: UUID,
        idempotency_key: str | None = None,
        processing_backend: str = "simulator",
        provider_account_id: str | None = None,
        payment_method: str | None = None,
    ) -> Payment:
        request_hash = self._hash_payload(payload) if idempotency_key is not None else None

        if idempotency_key is not None:
            existing_key = self.repository.get_idempotency_key(merchant_id, idempotency_key)
            if existing_key is not None:
                if existing_key.request_hash != request_hash:
                    raise IdempotencyConflictError(idempotency_key)
                replayed = self.repository.get(existing_key.payment_id, merchant_id)
                if replayed is not None:
                    return replayed

        payment = Payment(
            id=uuid4(),
            merchant_id=merchant_id,
            merchant_reference=payload.merchant_reference,
            amount=payload.amount,
            currency=payload.currency,
            customer_email=payload.customer_email,
            description=payload.description,
            status=PaymentStatus.PENDING,
        )
        self._prepare_processing(payment, processing_backend, provider_account_id, payment_method)
        self.repository.add(payment)

        try:
            self.repository.flush()
            self.publisher.publish(build_payment_created_event(payment))
            if idempotency_key is not None:
                self.repository.add_idempotency_key(
                    IdempotencyKey(
                        merchant_id=merchant_id,
                        key=idempotency_key,
                        request_hash=request_hash,
                        payment_id=payment.id,
                    )
                )
            self.repository.commit()
        except IntegrityError as exc:
            self.repository.rollback()
            # A concurrent request may have inserted the same idempotency key
            # between our lookup and commit. Treat that as a replay instead of a
            # duplicate-merchant conflict.
            if idempotency_key is not None:
                existing_key = self.repository.get_idempotency_key(merchant_id, idempotency_key)
                if existing_key is not None and existing_key.request_hash != request_hash:
                    raise IdempotencyConflictError(idempotency_key) from exc
                if existing_key is not None:
                    replayed = self.repository.get(existing_key.payment_id, merchant_id)
                    if replayed is not None:
                        return replayed
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if getattr(exc.orig, "sqlstate", None) not in {None, "23505"} or (
                constraint is not None and constraint not in {
                    "uq_payments_merchant_reference", "uq_idempotency_keys_merchant_key",
                }
            ):
                raise
            raise PaymentAlreadyExistsError(payload.merchant_reference) from exc
        except Exception:
            self.repository.rollback()
            raise

        self.repository.refresh(payment)
        log_event(
            logger,
            "payment_created",
            payment_id=payment.id,
            status=payment.status.value,
        )
        return payment

    def list_payments(self, merchant_id: UUID) -> list[Payment]:
        return self.repository.list_payments(merchant_id)

    def get_payment(self, payment_id: UUID, merchant_id: UUID) -> Payment | None:
        payment = self.repository.get(payment_id, merchant_id)
        if payment is None or payment.merchant_id != merchant_id:
            return None
        return payment

    def require_payment(self, payment_id: UUID, merchant_id: UUID) -> Payment:
        payment = self.get_payment(payment_id, merchant_id)
        if payment is None:
            raise PaymentNotFoundError(payment_id)
        return payment

    def get_payment_events(self, payment_id: UUID, merchant_id: UUID) -> list[PaymentEvent]:
        self.require_payment(payment_id, merchant_id)
        return self.repository.list_events(payment_id, merchant_id)

    def get_metrics(self, merchant_id: UUID) -> PaymentMetrics:
        return build_payment_metrics(self.repository.get_status_counts(merchant_id))

    def get_processing_snapshot(
        self, payment_id: UUID, merchant_id: UUID
    ) -> PaymentProcessingSnapshot | None:
        payment = self.get_payment(payment_id, merchant_id)
        if payment is None:
            return None

        latest_event = self.repository.get_latest_event(payment_id, merchant_id)
        return build_processing_snapshot(payment, latest_event)
