"""Evaluate one durable Stripe contract without a SQL transaction or state writes."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import re
import time

from app.repositories.payment_repository import ProviderOperation
from app.services.stripe_gateway import GatewayResult, GatewayStatus, PaymentGateway, STRIPE_API_VERSION, to_minor_units


@dataclass(frozen=True)
class StripeProcessingDecision:
    state: str
    provider_status: str | None = None
    provider_reference: str | None = None
    reason: str | None = None


class StripePaymentProcessor:
    """Reuse the original gateway/processor boundary, never an attempt-derived key."""
    def __init__(self, *, gateway: PaymentGateway, request_budget_seconds: float = 41) -> None:
        if not 0 <= request_budget_seconds <= 121:
            raise ValueError("Invalid provider request budget.")
        self.gateway = gateway
        self.request_budget_seconds = request_budget_seconds

    def evaluate(self, operation: ProviderOperation) -> StripeProcessingDecision:
        now = operation.claimed_at + timedelta(seconds=max(0, time.monotonic() - operation.claimed_monotonic))
        try:
            snapshot = json.loads(operation.request)
            params = snapshot["params"]
            expected = {
                "operation": "payment_intent.create_confirm", "api_version": STRIPE_API_VERSION,
                "account_id": operation.account_id, "livemode": False,
                "params": {"amount": to_minor_units(operation.amount, operation.currency),
                    "currency": operation.currency.lower(), "payment_method": params["payment_method"],
                    "confirm": True, "capture_method": "automatic",
                    "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                    "metadata": {"merchant_id": str(operation.merchant_id), "payment_id": str(operation.payment_id)}},
            }
            valid = (snapshot == expected and json.dumps(snapshot, sort_keys=True, separators=(",", ":")) == operation.request
                and hashlib.sha256(operation.request.encode("utf-8")).hexdigest() == operation.request_hash
                and operation.idempotency_key == f"pp:stripe:v1:{operation.merchant_id}:{operation.payment_id}"
                and re.fullmatch(r"pm_[A-Za-z0-9_]{1,250}", params["payment_method"]) is not None)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            return StripeProcessingDecision("review_required", reason="invalid_stored_contract")
        budget = timedelta(seconds=self.request_budget_seconds)
        if now + budget >= operation.lease_until:
            return StripeProcessingDecision("unknown", reason="insufficient_lease_budget")
        if operation.provider_reference is None and now + budget >= operation.retry_until:
            return StripeProcessingDecision("review_required", reason="unsafe_retry_window")
        try:
            if operation.provider_reference is not None:
                result = self.gateway.retrieve_payment(operation.provider_reference)
            else:
                result = self.gateway.create_payment(amount=Decimal(params["amount"]) / 100,
                    currency=params["currency"], payment_method=params["payment_method"],
                    idempotency_key=operation.idempotency_key, metadata=params["metadata"],
                    request_deadline=operation.claimed_monotonic +
                        (min(operation.retry_until, operation.lease_until) - operation.claimed_at).total_seconds()
                        - self.request_budget_seconds / 2)
        except Exception:
            # Transport surprises are uncertain, never evidence of a rejected charge.
            return StripeProcessingDecision("unknown", reason="provider_uncertain")
        if not isinstance(result, GatewayResult):
            return StripeProcessingDecision("unknown", reason="provider_uncertain")
        if result.status in {GatewayStatus.CONFIGURATION_ERROR, GatewayStatus.INVALID_REQUEST}:
            return StripeProcessingDecision("review_required", reason="provider_configuration_or_request")
        if result.status == GatewayStatus.UNKNOWN:
            if result.error_code == "provider_not_found" and operation.provider_reference is not None:
                return StripeProcessingDecision("review_required", reason="provider_reference_not_found")
            return StripeProcessingDecision("unknown", reason="provider_uncertain")
        try:
            tags = dict(result.metadata)
        except (TypeError, ValueError):
            return StripeProcessingDecision("review_required", reason="provider_evidence_mismatch")
        evidence = (result.account_id == operation.account_id and result.livemode is False
            and isinstance(result.provider_reference, str) and re.fullmatch(r"pi_[A-Za-z0-9_]{1,250}", result.provider_reference) is not None
            and (operation.provider_reference is None or result.provider_reference == operation.provider_reference)
            and type(result.amount_minor) is int and result.amount_minor == params["amount"]
            and result.currency == params["currency"] and type(result.amount_received) is int
            and 0 <= result.amount_received <= result.amount_minor
            and all(tags.get(key) == value for key, value in params["metadata"].items()))
        if not evidence:
            return StripeProcessingDecision("review_required", reason="provider_evidence_mismatch")
        state = result.provider_status
        if state == "succeeded" and result.status == GatewayStatus.SUCCEEDED and result.amount_received == params["amount"]:
            return StripeProcessingDecision("succeeded", state, result.provider_reference)
        if state in {"canceled", "requires_payment_method"} and result.amount_received == 0 and result.status in {GatewayStatus.DECLINED, GatewayStatus.PENDING}:
            return StripeProcessingDecision("failed", state, result.provider_reference, "provider_declined")
        if state in {"processing", "requires_action", "requires_confirmation", "requires_capture"} and result.status == GatewayStatus.PENDING:
            return StripeProcessingDecision("requires_action" if state == "requires_action" else "pending", state, result.provider_reference)
        return StripeProcessingDecision("review_required", reason="provider_evidence_mismatch")
