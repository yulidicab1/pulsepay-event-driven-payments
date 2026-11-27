"""Public, allowlisted provider fields; no credentials or execution contract."""
from collections.abc import Mapping
import re
from typing import Literal

from pydantic import BaseModel, model_validator

ProviderState = Literal["prepared", "in_flight", "pending", "requires_action", "unknown", "review_required", "succeeded", "failed"]
ProviderStatus = Literal["succeeded", "canceled", "processing", "requires_payment_method", "requires_confirmation", "requires_action", "requires_capture"]
STATES = {"prepared", "in_flight", "pending", "requires_action", "unknown", "review_required", "succeeded", "failed"}
STATUSES = {"succeeded", "canceled", "processing", "requires_payment_method", "requires_confirmation", "requires_action", "requires_capture"}

# This is an intentional disclosure boundary: new internal reasons require review.
PUBLIC_NOTES = {
    "provider_uncertain": "The provider result has not been confirmed yet.",
    "insufficient_lease_budget": "Processing will resume with a new execution lease.",
    "unsafe_retry_window": "Automatic retries have stopped because the safe retry window has ended.",
    "invalid_stored_contract": "The stored operation requires an operator review.",
    "provider_configuration_or_request": "Automatic processing is paused for a configuration review.",
    "provider_reference_not_found": "The provider reference requires an operator review.",
    "provider_evidence_mismatch": "The provider response needs verification before the payment can be confirmed.",
    "provider_reference_conflict": "The provider reference requires an operator review.",
    "provider_declined": "The provider confirmed a decline or cancellation.",
}
SIMULATOR_NOTES = {
    "Deterministic payment rejection rule triggered",
    "Deterministic retry rule triggered on first attempt",
    "Random transient processing failure triggered",
    "Payment record no longer exists", "Retry scheduled", "Payment processing failed",
}


def public_processing_note(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return "Processing details require an operator review."
    if value in SIMULATOR_NOTES or value in PUBLIC_NOTES.values() or value == "Processing details require an operator review.":
        return value
    return PUBLIC_NOTES.get(value, "Processing details require an operator review.")


class PaymentProviderInfo(BaseModel):
    processing_backend: Literal["simulator", "stripe"] | None = None
    provider_reference: str | None = None
    provider_state: ProviderState | None = None
    provider_status: ProviderStatus | None = None

    @model_validator(mode="before")
    @classmethod
    def public_provider_fields(cls, value):
        data = dict(value) if isinstance(value, Mapping) else {
            name: getattr(value, name, None) for name in cls.model_fields
        }
        if data.get("processing_backend") != "stripe":
            if data.get("processing_backend") != "simulator":
                data["processing_backend"] = None
            for name in ("provider_reference", "provider_state", "provider_status"):
                data[name] = None
        else:
            reference = data.get("provider_reference")
            if not isinstance(reference, str) or re.fullmatch(r"pi_[A-Za-z0-9_]{1,250}", reference) is None:
                data["provider_reference"] = None
            if data.get("provider_state") not in STATES:
                data["provider_state"] = "review_required"
            if data.get("provider_status") not in STATUSES:
                data["provider_status"] = None
        return data
