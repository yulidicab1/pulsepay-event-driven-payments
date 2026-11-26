"""Pinned SDK contract tests with fake HTTP only; no Stripe credentials/services."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
import json
import logging
import socket
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr, ValidationError
import pytest
import stripe

from app.core.config import Settings
from app.services.stripe_gateway import GatewayStatus, STRIPE_API_VERSION, StripeGateway, to_minor_units

TEST_KEY = "sk_test_synthetic_gateway_only"
PRIVATE_MARKER = "synthetic-private-response-value"
MERCHANT_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def no_network_or_sdk_console(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Gateway unit test attempted network access")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.delenv("STRIPE_LOG", raising=False)
    monkeypatch.setattr(stripe, "log", None)
    monkeypatch.setattr(stripe._util, "STRIPE_LOG", None)
    logger = logging.getLogger("stripe")
    previous, disabled = logger.level, logger.disabled
    yield
    logger.disabled = disabled
    logger.setLevel(previous)


def intent(**changes):
    data = {"object": "payment_intent", "id": "pi_synthetic", "status": "succeeded",
            "amount": 1200, "amount_received": 1200, "currency": "eur", "livemode": False,
            "metadata": {}, "client_secret": PRIVATE_MARKER, "customer": PRIVATE_MARKER}
    data.update(changes)
    return data


class FakeHTTP(stripe.HTTPClient):
    name = "synthetic-http"

    def __init__(self, responses=None):
        super().__init__()
        self.responses = list(responses or [(intent(), 200)])
        self.calls = []
        self.lock = Lock()

    def request(self, method, url, headers, post_data=None):
        with self.lock:
            self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": post_data})
            response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        body, status = response
        return json.dumps(body), status, {"request-id": "req_synthetic"}


def gateway(http=None, key=TEST_KEY):
    transport = http if http is not None else FakeHTTP()
    return StripeGateway(secret_key=SecretStr(key), http_client=transport), transport


def create(adapter, **changes):
    values = {"amount": Decimal("12.00"), "currency": "EUR", "payment_method": "pm_card_visa",
              "idempotency_key": "caller-owned-key-v1"}
    values.update(changes)
    return adapter.create_payment(**values)


def error(status, kind="api_error", **changes):
    payload = {"type": kind, "message": PRIVATE_MARKER}
    payload.update(changes)
    return ({"error": payload}, status)


def test_real_pinned_sdk_serializes_api_v1_and_preserves_requests():
    adapter, http = gateway()
    tags = {"merchant_id": MERCHANT_ID}
    http.responses = [(intent(metadata=tags), 200)]
    for _ in range(2):
        result = create(adapter, metadata=tags)
        assert result.status == GatewayStatus.SUCCEEDED
        assert result.amount_minor == result.amount_received == 1200
    assert stripe.VERSION == "15.3.1"
    assert tags == {"merchant_id": MERCHANT_ID}
    assert len(http.calls) == 2 and http.calls[0]["body"] == http.calls[1]["body"]
    for call in http.calls:
        assert call["method"] == "post"
        assert urlsplit(call["url"]).path == "/v1/payment_intents"
        assert call["headers"]["Idempotency-Key"] == "caller-owned-key-v1"
        assert call["headers"]["Stripe-Version"] == STRIPE_API_VERSION
        params = parse_qs(call["body"])
        assert params["amount"] == ["1200"] and params["currency"] == ["eur"]
        assert params["payment_method"] == ["pm_card_visa"] and params["confirm"] == ["true"]
        assert params["metadata[merchant_id]"] == [MERCHANT_ID]


def test_retrieve_is_get_only_and_returns_selected_evidence():
    adapter, http = gateway()
    result = adapter.retrieve_payment("pi_synthetic")
    assert result.status == GatewayStatus.SUCCEEDED
    assert result.provider_reference == "pi_synthetic"
    assert len(http.calls) == 1 and http.calls[0]["method"] == "get"
    assert urlsplit(http.calls[0]["url"]).path == "/v1/payment_intents/pi_synthetic"
    assert "Idempotency-Key" not in http.calls[0]["headers"]
    assert PRIVATE_MARKER not in repr(result)


@pytest.mark.parametrize("state,expected", [
    ("succeeded", GatewayStatus.SUCCEEDED), ("canceled", GatewayStatus.DECLINED),
    ("processing", GatewayStatus.PENDING), ("requires_action", GatewayStatus.PENDING),
    ("requires_capture", GatewayStatus.PENDING), ("requires_confirmation", GatewayStatus.PENDING),
    ("requires_payment_method", GatewayStatus.PENDING),
])
def test_provider_states_are_not_confused_with_new_charge_or_failure(state, expected):
    adapter, http = gateway(FakeHTTP([(intent(status=state, amount_received=1200 if state == "succeeded" else 0), 200)]))
    result = create(adapter)
    assert result.status == expected and result.provider_status == state
    assert len(http.calls) == 1


@pytest.mark.parametrize("status,kind,expected,code", [
    (400, "invalid_request_error", GatewayStatus.INVALID_REQUEST, "provider_invalid_request"),
    (400, "idempotency_error", GatewayStatus.UNKNOWN, "idempotency_conflict"),
    (401, "invalid_request_error", GatewayStatus.CONFIGURATION_ERROR, "provider_access_denied"),
    (403, "invalid_request_error", GatewayStatus.CONFIGURATION_ERROR, "provider_access_denied"),
    (404, "invalid_request_error", GatewayStatus.UNKNOWN, "provider_not_found"),
    (409, "invalid_request_error", GatewayStatus.UNKNOWN, "idempotency_conflict"),
    (429, "rate_limit_error", GatewayStatus.UNKNOWN, "rate_limited"),
    (500, "api_error", GatewayStatus.UNKNOWN, "provider_error"),
    (502, "api_error", GatewayStatus.UNKNOWN, "provider_error"),
    (503, "api_error", GatewayStatus.UNKNOWN, "provider_error"),
])
def test_http_errors_are_controlled_and_never_retried_implicitly(status, kind, expected, code, caplog, capsys):
    adapter, http = gateway(FakeHTTP([error(status, kind)]))
    caplog.set_level(logging.DEBUG)
    result = create(adapter)
    assert result.status == expected and result.error_code == code
    assert len(http.calls) == 1
    captured = capsys.readouterr()
    for marker in (PRIVATE_MARKER, TEST_KEY):
        assert marker not in repr(result) + caplog.text + captured.out + captured.err


@pytest.mark.parametrize("message", ["synthetic network interruption", "synthetic socket timeout"])
def test_network_and_timeout_are_unknown_and_retry_keeps_the_key(message):
    adapter, http = gateway(FakeHTTP([stripe.APIConnectionError(message), (intent(), 200)]))
    assert create(adapter).error_code == "connection_error"
    assert create(adapter).status == GatewayStatus.SUCCEEDED
    assert len(http.calls) == 2
    assert http.calls[0]["body"] == http.calls[1]["body"]
    assert {call["headers"]["Idempotency-Key"] for call in http.calls} == {"caller-owned-key-v1"}


def test_card_error_needs_evidence_and_never_exports_error_data():
    partial = {"id": "pi_synthetic", "status": "requires_payment_method"}
    verified = intent(status="requires_payment_method", amount_received=0)
    for record, expected in [(None, GatewayStatus.UNKNOWN), (partial, GatewayStatus.UNKNOWN), (verified, GatewayStatus.DECLINED)]:
        adapter, http = gateway(FakeHTTP([error(402, "card_error", code="card_declined", payment_intent=record)]))
        result = create(adapter)
        assert result.status == expected
        assert PRIVATE_MARKER not in repr(result)
        assert len(http.calls) == 1


def test_not_found_during_retrieval_never_falls_back_to_create():
    adapter, http = gateway(FakeHTTP([error(404, "invalid_request_error")]))
    result = adapter.retrieve_payment("pi_synthetic")
    assert result.status == GatewayStatus.UNKNOWN and result.error_code == "provider_not_found"
    assert all(call["method"] == "get" for call in http.calls)


@pytest.mark.parametrize("changes", [
    {"livemode": True}, {"livemode": None}, {"status": "future_state"}, {"status": []},
    {"object": "customer"}, {"id": "cus_synthetic"}, {"currency": "jpy"}, {"currency": "usd"}, {"currency": []},
    {"status": "canceled", "amount_received": 1200},
    {"amount": 1300}, {"amount": True}, {"amount_received": 1199},
    {"metadata": {"merchant_id": "not-a-uuid"}}, {"metadata": []},
])
def test_live_malformed_or_inconsistent_success_is_rejected(changes):
    adapter, _ = gateway(FakeHTTP([(intent(**changes), 200)]))
    result = create(adapter)
    assert result.status in {GatewayStatus.UNKNOWN, GatewayStatus.CONFIGURATION_ERROR}
    assert result.status != GatewayStatus.SUCCEEDED
    if changes.get("livemode") is True:
        assert result.status == GatewayStatus.CONFIGURATION_ERROR
        assert result.error_code == "live_mode_forbidden"


def test_response_reference_and_metadata_must_match_request():
    adapter, _ = gateway(FakeHTTP([(intent(id="pi_other"), 200)]))
    assert adapter.retrieve_payment("pi_synthetic").error_code == "invalid_response"
    adapter, _ = gateway()
    assert create(adapter, metadata={"merchant_id": MERCHANT_ID}).error_code == "invalid_response"


def test_response_omits_unapproved_metadata_and_private_fields():
    adapter, _ = gateway(FakeHTTP([(intent(metadata={"merchant_id": MERCHANT_ID, "private": PRIVATE_MARKER}), 200)]))
    result = create(adapter)
    assert result.metadata == (("merchant_id", MERCHANT_ID),)
    assert PRIVATE_MARKER not in repr(result)


@pytest.mark.parametrize("amount,expected", [("0.01", 1), ("12.3400", 1234), ("999999.99", 99999999)])
@pytest.mark.parametrize("currency", ["EUR", "usd", "GBP"])
def test_exact_money_conversion_independent_of_decimal_context(amount, expected, currency):
    with localcontext() as context:
        context.prec = 2
        assert to_minor_units(Decimal(amount), currency) == expected


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"),
    Decimal("0.001"), Decimal("1.230000000000000000000000001"), Decimal("1000000"),
    Decimal("1E-999999"), 1.23, "12.00", None])
def test_invalid_amounts_never_call_sdk(amount):
    adapter, http = gateway()
    assert create(adapter, amount=amount).status == GatewayStatus.INVALID_REQUEST
    assert not http.calls


@pytest.mark.parametrize("changes", [
    {"currency": "JPY"}, {"currency": None}, {"payment_method": "raw-card-data"},
    {"idempotency_key": ""}, {"idempotency_key": None}, {"idempotency_key": "x" * 256},
    {"idempotency_key": " space "}, {"idempotency_key": "header\r\ninjection"},
    {"idempotency_key": "unicode-ñ"}, {"metadata": {"email": "synthetic@example.com"}},
    {"metadata": {"merchant_id": PRIVATE_MARKER}},
])
def test_invalid_requests_are_rejected_without_mutation_or_sdk_calls(changes):
    adapter, http = gateway()
    assert create(adapter, **changes).status == GatewayStatus.INVALID_REQUEST
    assert not http.calls


@pytest.mark.parametrize("reference", ["", "pi_", "../../payment_intents", "pi_test?secret=synthetic", None])
def test_invalid_retrieval_ids_never_issue_any_request(reference):
    adapter, http = gateway()
    assert adapter.retrieve_payment(reference).status == GatewayStatus.INVALID_REQUEST
    assert not http.calls


def test_concurrent_identical_calls_preserve_caller_key_and_parameters():
    adapter, http = gateway()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: create(adapter), range(16)))
    assert all(result.status == GatewayStatus.SUCCEEDED for result in results)
    assert len(http.calls) == 16
    assert len({call["body"] for call in http.calls}) == 1
    assert {call["headers"]["Idempotency-Key"] for call in http.calls} == {"caller-owned-key-v1"}


@pytest.mark.parametrize("key", ["", "sk_live_synthetic_only", "rk_live_synthetic_only", "pk_test_synthetic_only", "arbitrary", "sk_test_with space"])
def test_invalid_or_live_keys_are_rejected_before_client_construction(key, monkeypatch):
    monkeypatch.setattr(stripe, "StripeClient", lambda *a, **k: pytest.fail("Invalid key reached SDK"))
    with pytest.raises(ValueError) as failure:
        gateway(key=key)
    assert key not in str(failure.value) if key else True


def test_restricted_test_key_and_protected_configuration(tmp_path):
    key_file = tmp_path / "synthetic-stripe-key"
    key_file.write_text("rk_test_synthetic_gateway_only")
    config = Settings(_env_file=None, stripe_secret_key_file=key_file)
    protected = config.get_stripe_secret_key()
    assert isinstance(protected, SecretStr) and protected.get_secret_value() not in repr(config)
    StripeGateway(secret_key=protected, http_client=FakeHTTP())


@pytest.mark.parametrize("options", [{}, {"stripe_secret_key": "sk_live_synthetic_only"},
    {"stripe_secret_key_file": "/nonexistent/synthetic-file"},
    {"stripe_secret_key": TEST_KEY, "stripe_secret_key_file": "/nonexistent/synthetic-file"}])
def test_missing_invalid_or_conflicting_configuration_fails_safely(options):
    config = Settings(_env_file=None, **options)
    with pytest.raises((ValueError, RuntimeError)) as failure:
        config.get_stripe_secret_key()
    assert TEST_KEY not in str(failure.value)


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan"), float("inf")])
def test_timeout_configuration_is_bounded(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stripe_timeout_seconds=timeout)
    with pytest.raises(ValueError):
        StripeGateway(secret_key=SecretStr(TEST_KEY), timeout_seconds=timeout, http_client=FakeHTTP())


def test_mode_live_is_invalid_and_default_does_not_activate_integration():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stripe_mode="live")
    assert Settings(_env_file=None).stripe_mode == "test"


@pytest.mark.parametrize("source", ["environment", "cached-environment", "sdk", "logger"])
def test_verbose_sdk_logging_is_rejected_before_io(source, monkeypatch, capsys):
    adapter, http = gateway()
    if source == "environment":monkeypatch.setenv("STRIPE_LOG", "debug")
    elif source == "cached-environment":monkeypatch.setattr(stripe._util, "STRIPE_LOG", "info")
    elif source == "sdk":monkeypatch.setattr(stripe, "log", "debug")
    else:
        logger = logging.getLogger("stripe")
        logger.disabled = False
        logger.setLevel(logging.INFO)
    assert create(adapter).error_code == "unsafe_sdk_logging"
    assert not http.calls
    assert not capsys.readouterr().err


@pytest.mark.parametrize("error_name", ["Timeout", "ConnectionError", "SSLError"])
def test_real_requests_transport_normalizes_timeout_without_network(error_name, caplog, capsys):
    import requests
    class TimeoutSession:
        def __init__(self):
            self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append(kwargs)
            raise getattr(requests.exceptions, error_name)(PRIVATE_MARKER)
    session = TimeoutSession()
    client = stripe.RequestsClient(timeout=7, session=session, verify_ssl_certs=True)
    adapter = StripeGateway(secret_key=SecretStr(TEST_KEY), http_client=client)
    result = create(adapter)
    assert result.status == GatewayStatus.UNKNOWN and result.error_code == "connection_error"
    assert len(session.calls) == 1
    assert session.calls[0]["timeout"] == 7 and session.calls[0]["verify"] is not False
    captured = capsys.readouterr()
    assert PRIVATE_MARKER not in repr(result) + caplog.text + captured.out + captured.err


def test_default_http_client_has_explicit_timeout_and_tls_verification(monkeypatch):
    options = []
    def transport(**kwargs):
        options.append(kwargs)
        return FakeHTTP()
    monkeypatch.setattr(stripe, "RequestsClient", transport)
    adapter = StripeGateway(secret_key=SecretStr(TEST_KEY), timeout_seconds=7)
    assert create(adapter).status == GatewayStatus.SUCCEEDED
    assert options == [{"timeout": 7, "verify_ssl_certs": True}]


def test_error_envelope_never_reports_success_and_preserves_only_id_candidate():
    records = [intent(), "pi_synthetic", intent(status="requires_action", amount_received=0), intent(livemode=True)]
    statuses = [GatewayStatus.UNKNOWN, GatewayStatus.UNKNOWN, GatewayStatus.PENDING, GatewayStatus.CONFIGURATION_ERROR]
    for record, expected in zip(records, statuses):
        adapter, _ = gateway(FakeHTTP([error(402, "card_error", payment_intent=record)]))
        result = create(adapter)
        assert result.status == expected
        assert PRIVATE_MARKER not in repr(result)
        if isinstance(record, str):
            assert result.provider_reference == record


def test_concurrent_distinct_keys_do_not_share_mutable_request_options():
    adapter, http = gateway()
    keys = [f"synthetic-operation-{i}" for i in range(16)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda key: create(adapter, idempotency_key=key), keys))
    assert all(result.status == GatewayStatus.SUCCEEDED for result in results)
    assert sorted(call["headers"]["Idempotency-Key"] for call in http.calls) == sorted(keys)


def test_missing_idempotency_argument_and_raw_key_fail_before_io():
    adapter, http = gateway()
    with pytest.raises(TypeError):
        adapter.create_payment(amount=Decimal("12.00"), currency="EUR", payment_method="pm_card_visa")
    assert not http.calls
    with pytest.raises(ValueError):
        StripeGateway(secret_key=TEST_KEY, http_client=http)


@pytest.mark.parametrize("body", [None, [], {"object": "payment_intent"}])
def test_malformed_success_envelopes_are_unknown(body):
    adapter, _ = gateway(FakeHTTP([(body, 200)]))
    assert create(adapter).status == GatewayStatus.UNKNOWN


def test_constructor_rejects_sdk_console_logging(monkeypatch):
    monkeypatch.setenv("STRIPE_LOG", "debug")
    with pytest.raises(ValueError, match="verbose logging"):
        StripeGateway(secret_key=SecretStr(TEST_KEY), http_client=FakeHTTP())


def test_empty_secret_file_is_controlled(tmp_path):
    path = tmp_path / "empty-synthetic-key"
    path.write_text("")
    with pytest.raises(RuntimeError, match="empty"):
        Settings(_env_file=None, stripe_secret_key_file=path).get_stripe_secret_key()


def test_explicit_client_key_does_not_use_sdk_global_credentials(monkeypatch):
    monkeypatch.setattr(stripe, "api_key", "sk_live_synthetic_global_not_used")
    adapter, http = gateway()
    assert create(adapter).status == GatewayStatus.SUCCEEDED
    assert http.calls[0]["headers"]["Authorization"] == "Bearer " + TEST_KEY


def test_success_has_frozen_sanitized_result():
    from dataclasses import FrozenInstanceError
    adapter, _ = gateway()
    result = create(adapter)
    with pytest.raises(FrozenInstanceError):
        result.provider_reference = "pi_other"
    assert TEST_KEY not in repr(adapter) + repr(result)
