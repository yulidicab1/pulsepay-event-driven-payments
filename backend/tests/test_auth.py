"""Tests for multi-tenant API key authentication and merchant isolation."""

from pydantic import SecretStr

from app.repositories import SqlAlchemyMerchantRepository
from app.services.merchant_service import MerchantService


def _payload(merchant_reference: str = "auth-order-1") -> dict:
    return {
        "merchant_reference": merchant_reference,
        "amount": "50.00",
        "currency": "usd",
        "customer_email": "ops@example.com",
    }


def test_request_without_api_key_is_unauthorized(anon_client):
    response = anon_client.post("/api/v1/payments", json=_payload())
    assert response.status_code == 401


def test_request_with_invalid_api_key_is_unauthorized(anon_client):
    response = anon_client.post(
        "/api/v1/payments",
        json=_payload(),
        headers={"Authorization": "Bearer sk_test_does_not_exist"},
    )
    assert response.status_code == 401


def test_valid_api_key_is_authorized(client):
    response = client.post("/api/v1/payments", json=_payload())
    assert response.status_code == 201


def test_merchant_cannot_see_another_merchants_payment(anon_client, db_session):
    # Merchant A creates a payment.
    service = MerchantService(repository=SqlAlchemyMerchantRepository(db_session))
    _, key_a = service.create_merchant("Merchant A", api_key="sk_test_merchant_a")
    _, key_b = service.create_merchant("Merchant B", api_key="sk_test_merchant_b")

    created = anon_client.post(
        "/api/v1/payments",
        json=_payload("shared-ref"),
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert created.status_code == 201
    payment_id = created.json()["id"]

    # Merchant B must not see it by id nor in its listing.
    detail_b = anon_client.get(
        f"/api/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    list_b = anon_client.get(
        "/api/v1/payments",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert detail_b.status_code == 404
    assert list_b.json() == []

    # Merchant A still sees its own payment.
    detail_a = anon_client.get(
        f"/api/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert detail_a.status_code == 200


def test_same_reference_allowed_across_different_merchants(anon_client, db_session):
    service = MerchantService(repository=SqlAlchemyMerchantRepository(db_session))
    _, key_a = service.create_merchant("Merchant A", api_key="sk_test_a2")
    _, key_b = service.create_merchant("Merchant B", api_key="sk_test_b2")

    first = anon_client.post(
        "/api/v1/payments",
        json=_payload("same-ref"),
        headers={"Authorization": f"Bearer {key_a}"},
    )
    second = anon_client.post(
        "/api/v1/payments",
        json=_payload("same-ref"),
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert first.status_code == 201
    assert second.status_code == 201  # per-merchant uniqueness, not global


def test_merchant_onboarding_requires_admin_token(anon_client, monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.admin_api_token", SecretStr("synthetic-admin-secret"))

    forbidden = anon_client.post("/api/v1/merchants", json={"name": "Acme"})
    assert forbidden.status_code == 403

    created = anon_client.post(
        "/api/v1/merchants",
        json={"name": "Acme", "issue_api_key": True},
        headers={"X-Admin-Token": "synthetic-admin-secret"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Acme"
    assert body["api_key"].startswith("sk_test_")

    # The freshly minted key works for creating a payment.
    used = anon_client.post(
        "/api/v1/payments",
        json=_payload("acme-1"),
        headers={"Authorization": f"Bearer {body['api_key']}"},
    )
    assert used.status_code == 201


def test_merchant_onboarding_disabled_without_admin_token_configured(anon_client):
    # Default settings leave admin_api_token unset, so onboarding is disabled.
    response = anon_client.post(
        "/api/v1/merchants",
        json={"name": "Acme", "issue_api_key": True},
        headers={"X-Admin-Token": "whatever"},
    )
    assert response.status_code == 403
