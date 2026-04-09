from app.models.payment import PaymentEvent


def test_health_check(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_payment_persists_and_publishes_event(client, db_session):
    payload = {
        "merchant_reference": "order-1001",
        "amount": "149.99",
        "currency": "eur",
        "customer_email": "customer@example.com",
        "description": "VIP ticket purchase",
    }

    response = client.post("/api/v1/payments", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["merchant_reference"] == "order-1001"
    assert body["status"] == "PENDING"
    assert body["currency"] == "EUR"

    event = db_session.query(PaymentEvent).one()
    assert event.event_type == "payment.created"
    assert event.queue_name == "payment-events"
    assert event.payload["merchant_reference"] == "order-1001"


def test_create_payment_returns_conflict_for_duplicate_reference(client):
    payload = {
        "merchant_reference": "order-duplicate",
        "amount": "149.99",
        "currency": "usd",
        "customer_email": "customer@example.com",
    }

    first_response = client.post("/api/v1/payments", json=payload)
    second_response = client.post("/api/v1/payments", json=payload)

    assert first_response.status_code == 201
    assert second_response.status_code == 409
    assert "already exists" in second_response.json()["detail"]


def test_list_and_detail_endpoints_return_created_payment(client):
    create_response = client.post(
        "/api/v1/payments",
        json={
            "merchant_reference": "order-2001",
            "amount": "75.00",
            "currency": "usd",
            "customer_email": "buyer@example.com",
        },
    )
    payment_id = create_response.json()["id"]

    list_response = client.get("/api/v1/payments")
    detail_response = client.get(f"/api/v1/payments/{payment_id}")

    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert list_response.json()[0]["id"] == payment_id

    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == payment_id


def test_get_payment_returns_not_found_for_unknown_id(client):
    response = client.get("/api/v1/payments/84e8d21f-2547-494c-aaec-fd6a89fbe8f5")

    assert response.status_code == 404


def test_create_payment_normalizes_currency_and_persists_optional_description(client):
    response = client.post(
        "/api/v1/payments",
        json={
            "merchant_reference": "order-3001",
            "amount": "25.50",
            "currency": "gbp",
            "customer_email": "buyer@example.com",
            "description": "Membership renewal",
        },
    )

    assert response.status_code == 201
    assert response.json()["currency"] == "GBP"
    assert response.json()["description"] == "Membership renewal"


def test_create_payment_rejects_invalid_input(client):
    response = client.post(
        "/api/v1/payments",
        json={
            "merchant_reference": "x",
            "amount": "-1.00",
            "currency": "us",
            "customer_email": "invalid-email",
        },
    )

    assert response.status_code == 422


def test_create_payment_rejects_missing_required_fields(client):
    response = client.post(
        "/api/v1/payments",
        json={
            "merchant_reference": "order-4001",
            "currency": "USD",
        },
    )

    assert response.status_code == 422
