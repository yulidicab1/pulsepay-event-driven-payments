"""Tests for Idempotency-Key handling on payment creation."""


def _payload(merchant_reference: str = "idem-order-1", amount: str = "120.00") -> dict:
    return {
        "merchant_reference": merchant_reference,
        "amount": amount,
        "currency": "usd",
        "customer_email": "ops@example.com",
    }


def test_same_key_and_body_replays_the_same_payment(client):
    headers = {"Idempotency-Key": "key-123"}

    first = client.post("/api/v1/payments", json=_payload(), headers=headers)
    second = client.post("/api/v1/payments", json=_payload(), headers=headers)

    assert first.status_code == 201
    assert second.status_code in (200, 201)
    assert first.json()["id"] == second.json()["id"]

    # The replay must not create a second payment nor a second queue event.
    listing = client.get("/api/v1/payments")
    assert len(listing.json()) == 1

    events = client.get(f"/api/v1/payments/{first.json()['id']}/events")
    assert len(events.json()) == 1


def test_same_key_with_different_body_conflicts(client):
    headers = {"Idempotency-Key": "key-abc"}

    first = client.post("/api/v1/payments", json=_payload(amount="120.00"), headers=headers)
    conflict = client.post("/api/v1/payments", json=_payload(amount="999.00"), headers=headers)

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert "Idempotency-Key" in conflict.json()["detail"]

    # Only the first payment exists.
    listing = client.get("/api/v1/payments")
    assert len(listing.json()) == 1


def test_requests_without_key_keep_existing_behavior(client):
    # Two distinct references without a key create two payments.
    first = client.post("/api/v1/payments", json=_payload("no-key-1"))
    second = client.post("/api/v1/payments", json=_payload("no-key-2"))

    assert first.status_code == 201
    assert second.status_code == 201

    listing = client.get("/api/v1/payments")
    assert len(listing.json()) == 2

    # The same reference without a key still conflicts as before.
    duplicate = client.post("/api/v1/payments", json=_payload("no-key-1"))
    assert duplicate.status_code == 409


def test_different_keys_create_separate_payments(client):
    first = client.post(
        "/api/v1/payments",
        json=_payload("multi-1"),
        headers={"Idempotency-Key": "key-1"},
    )
    second = client.post(
        "/api/v1/payments",
        json=_payload("multi-2"),
        headers={"Idempotency-Key": "key-2"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    listing = client.get("/api/v1/payments")
    assert len(listing.json()) == 2
