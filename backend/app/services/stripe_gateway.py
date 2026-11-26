"""Test-only Stripe API v1 adapter. No persistence, retries or business state writes."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Context, Decimal, Inexact, InvalidOperation
from enum import Enum
import logging
import math
import os
import re
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import SecretStr

from app.core.config import validate_stripe_test_key

if TYPE_CHECKING:
    import stripe

STRIPE_API_VERSION = "2026-06-24.dahlia"
SUPPORTED_CURRENCIES = frozenset({"EUR", "USD", "GBP"})
METADATA_KEYS = frozenset({"merchant_id", "payment_id", "operation_id"})
PROVIDER_STATUSES = frozenset({
    "succeeded", "canceled", "processing", "requires_payment_method",
    "requires_confirmation", "requires_action", "requires_capture",
})


class GatewayStatus(str, Enum):
    SUCCEEDED = "succeeded"
    DECLINED = "declined"
    PENDING = "pending"
    UNKNOWN = "unknown"
    INVALID_REQUEST = "invalid_request"
    CONFIGURATION_ERROR = "configuration_error"


@dataclass(frozen=True)
class GatewayResult:
    status: GatewayStatus
    provider_reference: str | None = None
    provider_status: str | None = None
    amount_minor: int | None = None
    amount_received: int | None = None
    currency: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    error_code: str | None = None


class PaymentGateway(ABC):
    @abstractmethod
    def create_payment(self, *, amount: Decimal, currency: str, payment_method: str,
                       idempotency_key: str, metadata: Mapping[str, str] | None = None) -> GatewayResult:
        raise NotImplementedError

    @abstractmethod
    def retrieve_payment(self, provider_reference: str) -> GatewayResult:
        raise NotImplementedError


def to_minor_units(amount: Decimal, currency: str) -> int:
    """Exact conversion independent of the caller's Decimal context; never round."""
    if (
        not isinstance(currency, str) or currency.upper() not in SUPPORTED_CURRENCIES
        or not isinstance(amount, Decimal) or not amount.is_finite()
        or not Decimal("0.01") <= amount <= Decimal("999999.99")
    ):
        raise ValueError("Unsupported payment amount or currency.")
    context = Context(prec=12, traps=[Inexact, InvalidOperation])
    try:
        exact = amount.quantize(Decimal("0.01"), context=context)
        return int(exact.scaleb(2, context=context))
    except (Inexact, InvalidOperation):
        raise ValueError("Unsupported payment amount or currency.") from None


def _metadata(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) - METADATA_KEYS:
        raise ValueError("Invalid operation metadata.")
    result = dict(value)
    for item in result.values():
        if not isinstance(item, str):
            raise ValueError("Invalid operation metadata.")
        try:
            UUID(item)
        except ValueError:
            raise ValueError("Invalid operation metadata.") from None
    return result


def _reference(value: str, prefix: str) -> bool:
    return isinstance(value, str) and re.fullmatch(prefix + r"[A-Za-z0-9_]{1,250}", value) is not None


class StripeGateway(PaymentGateway):
    def __init__(self, *, secret_key: SecretStr, timeout_seconds: float = 10,
                 http_client: "stripe.HTTPClient | None" = None) -> None:
        validate_stripe_test_key(secret_key)
        if (
            isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30
        ):
            raise ValueError("Invalid Stripe request timeout.")
        import stripe
        self._sdk = stripe
        if self._console_logging():
            raise ValueError("Stripe SDK verbose logging must be disabled.")
        # The SDK logs raw response/error bodies at INFO/DEBUG. Console logging
        # bypasses Python logging, so reject that configuration separately.
        logging.getLogger("stripe").setLevel(logging.WARNING)
        self._client = stripe.StripeClient(
            secret_key.get_secret_value(), stripe_version=STRIPE_API_VERSION,
            max_network_retries=0,
            http_client=http_client if http_client is not None else stripe.RequestsClient(timeout=timeout_seconds, verify_ssl_certs=True),
        )

    def _console_logging(self) -> bool:
        # The pinned SDK caches STRIPE_LOG at import time as well as reading its
        # public stripe.log setting. Never let either path print raw responses.
        return any(value in {"debug", "info"} for value in (
            os.getenv("STRIPE_LOG"), self._sdk.log, self._sdk._util.STRIPE_LOG,
        ))

    def create_payment(self, *, amount: Decimal, currency: str, payment_method: str,
                       idempotency_key: str, metadata: Mapping[str, str] | None = None) -> GatewayResult:
        try:
            minor = to_minor_units(amount, currency)
            if (
                not _reference(payment_method, "pm_") or not isinstance(idempotency_key, str)
                or re.fullmatch(r"[!-~]{1,255}", idempotency_key) is None
            ):
                raise ValueError("Invalid payment request.")
            tags = _metadata(metadata if metadata is not None else {})
        except ValueError:
            return GatewayResult(GatewayStatus.INVALID_REQUEST, error_code="invalid_request")
        params = {
            "amount": minor, "currency": currency.lower(), "payment_method": payment_method,
            "confirm": True, "capture_method": "automatic",
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        }
        if tags:
            params["metadata"] = tags
        return self._request(params=params, idempotency_key=idempotency_key)

    def retrieve_payment(self, provider_reference: str) -> GatewayResult:
        if not _reference(provider_reference, "pi_"):
            return GatewayResult(GatewayStatus.INVALID_REQUEST, error_code="invalid_request")
        return self._request(provider_reference=provider_reference)

    def _request(self, *, provider_reference=None, params=None, idempotency_key=None) -> GatewayResult:
        if self._console_logging() or logging.getLogger("stripe").isEnabledFor(logging.INFO):
            return GatewayResult(GatewayStatus.CONFIGURATION_ERROR, error_code="unsafe_sdk_logging")
        try:
            if provider_reference is not None:
                intent = self._client.v1.payment_intents.retrieve(provider_reference)
            else:
                intent = self._client.v1.payment_intents.create(params=params, options={"idempotency_key": idempotency_key})
        except self._sdk.StripeError as exc:
            if isinstance(exc, self._sdk.CardError):
                error = exc.json_body.get("error", {}) if isinstance(exc.json_body, dict) else {}
                candidate = error.get("payment_intent") if isinstance(error, dict) else None
                if isinstance(candidate, Mapping):
                    result = self._normalize(candidate, provider_reference, params)
                    if result.status == GatewayStatus.SUCCEEDED:
                        return GatewayResult(GatewayStatus.UNKNOWN, error_code="invalid_response")
                    if result.provider_status == "requires_payment_method" and result.amount_received == 0:
                        return replace(result, status=GatewayStatus.DECLINED, error_code="payment_declined")
                    return result
                return GatewayResult(GatewayStatus.UNKNOWN,
                    provider_reference=candidate if _reference(candidate, "pi_") else None, error_code="card_error")
            if isinstance(exc, (self._sdk.AuthenticationError, self._sdk.PermissionError)):
                return GatewayResult(GatewayStatus.CONFIGURATION_ERROR, error_code="provider_access_denied")
            if isinstance(exc, self._sdk.APIConnectionError):
                code = "connection_error"
            elif isinstance(exc, self._sdk.RateLimitError):
                code = "rate_limited"
            elif isinstance(exc, self._sdk.IdempotencyError) or exc.http_status == 409:
                code = "idempotency_conflict"
            elif exc.http_status == 404:
                code = "provider_not_found"
            elif isinstance(exc, self._sdk.InvalidRequestError):
                return GatewayResult(GatewayStatus.INVALID_REQUEST, error_code="provider_invalid_request")
            else:
                code = "provider_error"
            return GatewayResult(GatewayStatus.UNKNOWN, provider_reference=provider_reference, error_code=code)
        if isinstance(intent, self._sdk.StripeObject):
            intent = intent.to_dict()
        return self._normalize(intent, provider_reference, params)

    @staticmethod
    def _normalize(intent, expected_reference, params) -> GatewayResult:
        if not isinstance(intent, Mapping):
            return GatewayResult(GatewayStatus.UNKNOWN, error_code="invalid_response")
        if intent.get("livemode") is True:
            return GatewayResult(GatewayStatus.CONFIGURATION_ERROR, error_code="live_mode_forbidden")
        reference, state = intent.get("id"), intent.get("status")
        amount, received, currency = intent.get("amount"), intent.get("amount_received"), intent.get("currency")
        try:
            raw_tags = intent.get("metadata", {})
            if not isinstance(raw_tags, Mapping):
                raise ValueError
            tags = _metadata({key: value for key, value in raw_tags.items() if key in METADATA_KEYS})
            valid = (
                intent.get("object") == "payment_intent" and intent.get("livemode") is False
                and _reference(reference, "pi_") and state in PROVIDER_STATUSES
                and type(amount) is int and 0 < amount <= 99999999
                and type(received) is int and 0 <= received <= amount
                and currency in {"eur", "usd", "gbp"}
                and (expected_reference is None or reference == expected_reference)
                and (state != "succeeded" or received == amount)
                and (state != "canceled" or received == 0)
                and (params is None or (
                    amount == params["amount"] and currency == params["currency"]
                    and all(tags.get(key) == value for key, value in params.get("metadata", {}).items())
                ))
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            return GatewayResult(GatewayStatus.UNKNOWN, error_code="invalid_response")
        if state == "succeeded":
            status = GatewayStatus.SUCCEEDED
        elif state == "canceled":
            status = GatewayStatus.DECLINED
        else:
            status = GatewayStatus.PENDING
        return GatewayResult(status, reference, state, amount, received, currency, tuple(sorted(tags.items())),
                             "payment_canceled" if state == "canceled" else None)
