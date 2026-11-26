"""Control B unit contracts: synthetic data and no network."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import socket
import time
from uuid import uuid4
from unittest.mock import Mock

from pydantic import SecretStr, ValidationError
import pytest

from app.core.config import Settings
from app.models.payment import Payment
from app.repositories.payment_repository import ProviderOperation
from app.services.payment_service import PaymentService
from app.services.stripe_gateway import GatewayResult, GatewayStatus, PaymentGateway, StripeGateway
from app.services.stripe_processor import StripePaymentProcessor
from test_stripe_gateway import FakeHTTP, intent, TEST_KEY


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a, **k): pytest.fail("Stripe processor test attempted network")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def operation():
    payment = Payment(id=uuid4(), merchant_id=uuid4(), amount=Decimal("12.00"), currency="EUR")
    PaymentService._prepare_processing(payment, "stripe", "acct_syntheticA", "pm_card_visa")
    now = datetime.now(UTC)
    return ProviderOperation(payment.id, payment.merchant_id, payment.provider_account_id, uuid4(),
        now + timedelta(seconds=120), now, time.monotonic(), payment.amount, payment.currency,
        payment.provider_idempotency_key, payment.provider_request, payment.provider_request_hash,
        None, now + timedelta(hours=23), 1)


def evidence(op, **changes):
    result = GatewayResult(GatewayStatus.SUCCEEDED, "pi_synthetic", "succeeded", 1200, 1200, "eur",
        tuple(sorted(json.loads(op.request)["params"]["metadata"].items())), account_id=op.account_id, livemode=False)
    return replace(result, **changes)


@pytest.mark.parametrize("provider_state,received,expected", [
    ("succeeded",1200,"succeeded"), ("canceled",0,"failed"), ("requires_payment_method",0,"failed"),
    ("processing",0,"pending"), ("requires_action",0,"requires_action"),
    ("requires_capture",0,"pending"), ("requires_confirmation",0,"pending"),
])
def test_evidence_drives_financial_state(provider_state, received, expected):
    op = operation()
    status = GatewayStatus.SUCCEEDED if provider_state == "succeeded" else GatewayStatus.DECLINED if provider_state == "canceled" else GatewayStatus.PENDING
    gateway = Mock(spec=PaymentGateway)
    gateway.create_payment.return_value = evidence(op, status=status, provider_status=provider_state, amount_received=received)
    result = StripePaymentProcessor(gateway=gateway).evaluate(op)
    assert result.state == expected
    assert result.provider_reference == "pi_synthetic"


@pytest.mark.parametrize("changes", [
    {"account_id":"acct_other"}, {"livemode":True}, {"livemode":None},
    {"amount_minor":1300}, {"currency":"usd"}, {"amount_received":1199},
    {"metadata":()}, {"metadata":(("merchant_id",str(uuid4())),)},
    {"provider_reference":"invalid"}, {"provider_status":"processing"},
])
def test_mismatched_evidence_never_completes(changes):
    op = operation(); gateway=Mock(spec=PaymentGateway)
    gateway.create_payment.return_value = evidence(op, **changes)
    decision=StripePaymentProcessor(gateway=gateway).evaluate(op)
    assert decision.state == "review_required" and decision.provider_reference is None


def test_known_reference_uses_get_even_after_retry_window():
    op=replace(operation(),provider_reference="pi_synthetic",retry_until=datetime.now(UTC)-timedelta(days=1))
    gateway=Mock(spec=PaymentGateway);gateway.retrieve_payment.return_value=evidence(op)
    assert StripePaymentProcessor(gateway=gateway).evaluate(op).state == "succeeded"
    gateway.create_payment.assert_not_called();gateway.retrieve_payment.assert_called_once_with("pi_synthetic")


def test_wrong_retrieved_reference_is_rejected():
    op=replace(operation(),provider_reference="pi_original")
    gateway=Mock(spec=PaymentGateway);gateway.retrieve_payment.return_value=evidence(op)
    assert StripePaymentProcessor(gateway=gateway).evaluate(op).state == "review_required"
    gateway.create_payment.assert_not_called()


@pytest.mark.parametrize("field", ["window","lease","snapshot"])
def test_unsafe_emission_is_blocked_before_any_gateway_call(field):
    op=operation()
    if field == "window":op=replace(op,retry_until=op.claimed_at-timedelta(seconds=1))
    elif field == "lease":op=replace(op,lease_until=op.claimed_at+timedelta(seconds=1))
    else:op=replace(op,request="{}")
    gateway=Mock(spec=PaymentGateway)
    result=StripePaymentProcessor(gateway=gateway).evaluate(op)
    assert result.state in {"unknown","review_required"}
    gateway.create_payment.assert_not_called();gateway.retrieve_payment.assert_not_called()


@pytest.mark.parametrize("error", ["connection_error","provider_error","rate_limited","invalid_response","idempotency_conflict"])
def test_uncertain_errors_are_not_declines(error):
    op=operation();gateway=Mock(spec=PaymentGateway)
    gateway.create_payment.return_value=GatewayResult(GatewayStatus.UNKNOWN,error_code=error)
    assert StripePaymentProcessor(gateway=gateway).evaluate(op).state == "unknown"


def test_transport_exception_is_safe_and_preserves_request_on_retry():
    op=operation();gateway=Mock(spec=PaymentGateway)
    gateway.create_payment.side_effect=[TimeoutError("SYNTHETIC_PRIVATE_ERROR"),evidence(op)]
    processor=StripePaymentProcessor(gateway=gateway)
    decision=processor.evaluate(op)
    assert decision.state == "unknown" and "PRIVATE" not in repr(decision)
    assert processor.evaluate(op).state == "succeeded"
    assert gateway.create_payment.call_args_list[0] == gateway.create_payment.call_args_list[1]


@pytest.mark.parametrize("status", [GatewayStatus.CONFIGURATION_ERROR,GatewayStatus.INVALID_REQUEST])
def test_configuration_failure_requires_review_not_financial_failure(status):
    op=operation();gateway=Mock(spec=PaymentGateway)
    gateway.create_payment.return_value=GatewayResult(status,error_code="synthetic_error")
    assert StripePaymentProcessor(gateway=gateway).evaluate(op).state == "review_required"


def test_real_sdk_checks_current_account_and_binds_test_evidence():
    op=operation();tags=json.loads(op.request)["params"]["metadata"]
    http=FakeHTTP([({"object":"account","id":op.account_id},200),(intent(metadata=tags),200)])
    gateway=StripeGateway(secret_key=SecretStr(TEST_KEY),http_client=http,account_id=op.account_id)
    assert StripePaymentProcessor(gateway=gateway).evaluate(op).state == "succeeded"
    assert [call["method"] for call in http.calls] == ["get","post"]
    assert http.calls[0]["url"].endswith("/v1/account")
    assert http.calls[1]["headers"]["Idempotency-Key"] == op.idempotency_key


def test_real_sdk_wrong_current_account_never_posts():
    op=operation();http=FakeHTTP([({"object":"account","id":"acct_other"},200)])
    gateway=StripeGateway(secret_key=SecretStr(TEST_KEY),http_client=http,account_id=op.account_id)
    assert StripePaymentProcessor(gateway=gateway).evaluate(op).state == "review_required"
    assert [call["method"] for call in http.calls] == ["get"]


@pytest.mark.parametrize("kwargs", [
    {"stripe_worker_enabled":True}, {"stripe_account_id":"invalid"},
    {"stripe_lease_seconds":10,"stripe_timeout_seconds":10}, {"stripe_mode":"live"},
])
def test_invalid_worker_configuration_fails_closed(kwargs):
    with pytest.raises(ValidationError):Settings(_env_file=None,**kwargs)



def test_account_lookup_delay_cannot_emit_after_request_deadline(monkeypatch):
    op=operation();clock=[100.0]
    class DelayedAccount(FakeHTTP):
        def request(self,method,url,headers,post_data=None):
            response=super().request(method,url,headers,post_data)
            if url.endswith("/v1/account"):clock[0]=200.0
            return response
    http=DelayedAccount([({"object":"account","id":op.account_id},200)])
    gateway=StripeGateway(secret_key=SecretStr(TEST_KEY),http_client=http,account_id=op.account_id)
    monkeypatch.setattr("app.services.stripe_gateway.time.monotonic",lambda:clock[0])
    result=gateway.create_payment(amount=op.amount,currency=op.currency,payment_method="pm_card_visa",
        idempotency_key=op.idempotency_key,request_deadline=150.0)
    assert result.status==GatewayStatus.UNKNOWN and result.error_code=="request_deadline_elapsed"
    assert [call["method"] for call in http.calls]==["get"]



@pytest.mark.parametrize("status,expected", [(401,"review_required"),(403,"review_required"),(500,"unknown")])
def test_account_errors_never_reach_payment_intent_post(status,expected):
    op=operation();http=FakeHTTP([({"error":{"type":"api_error","message":"SYNTHETIC_PRIVATE"}},status)])
    gateway=StripeGateway(secret_key=SecretStr(TEST_KEY),http_client=http,account_id=op.account_id)
    assert StripePaymentProcessor(gateway=gateway).evaluate(op).state==expected
    assert [call["method"] for call in http.calls]==["get"]


@pytest.mark.parametrize("account", [True,1,"acct_bad space",""])
def test_invalid_account_context_is_controlled(account):
    with pytest.raises(ValueError,match="Invalid Stripe account"):
        StripeGateway(secret_key=SecretStr(TEST_KEY),http_client=FakeHTTP(),account_id=account)
