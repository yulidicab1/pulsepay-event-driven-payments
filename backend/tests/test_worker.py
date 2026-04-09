from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.api.deps import build_worker_service
from app.models.payment import Payment, PaymentEvent, PaymentStatus, QueueMessageStatus


def create_payment(client, merchant_reference: str, amount: str = "120.00") -> str:
    response = client.post(
        "/api/v1/payments",
        json={
            "merchant_reference": merchant_reference,
            "amount": amount,
            "currency": "usd",
            "customer_email": "ops@example.com",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_worker_completes_successful_payment(client, db_session):
    payment_id = UUID(create_payment(client, "order-success"))

    result = build_worker_service(db_session).process_available_events()

    payment = db_session.get(Payment, payment_id)
    event = db_session.query(PaymentEvent).one()

    assert result.claimed == 1
    assert result.completed == 1
    assert payment.status == PaymentStatus.COMPLETED
    assert event.status == QueueMessageStatus.COMPLETED
    assert event.attempts == 1


def test_worker_retries_retryable_payment(client, db_session):
    payment_id = UUID(create_payment(client, "order-retry"))

    first_result = build_worker_service(db_session).process_available_events()
    payment = db_session.get(Payment, payment_id)
    event = db_session.query(PaymentEvent).one()

    assert first_result.retried == 1
    assert payment.status == PaymentStatus.PENDING
    assert payment.retry_count == 1
    assert event.status == QueueMessageStatus.PENDING
    assert event.attempts == 1
    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
    assert event.available_at > now_utc_naive - timedelta(seconds=1)

    event.available_at = now_utc_naive - timedelta(seconds=1)
    db_session.commit()

    second_result = build_worker_service(db_session).process_available_events()
    payment = db_session.get(Payment, payment_id)
    event = db_session.query(PaymentEvent).one()

    assert second_result.completed == 1
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.retry_count == 1
    assert event.status == QueueMessageStatus.COMPLETED
    assert event.attempts == 2


def test_worker_fails_deterministically(client, db_session):
    payment_id = UUID(create_payment(client, "order-fail"))

    result = build_worker_service(db_session).process_available_events()

    payment = db_session.get(Payment, payment_id)
    event = db_session.query(PaymentEvent).one()

    assert result.failed == 1
    assert payment.status == PaymentStatus.FAILED
    assert payment.failure_reason == "Deterministic payment rejection rule triggered"
    assert event.status == QueueMessageStatus.FAILED


def test_worker_fails_after_retry_limit_is_reached(client, db_session, monkeypatch):
    payment_id = UUID(create_payment(client, "order-retry-limit"))

    monkeypatch.setattr("app.services.worker_service.settings.payment_max_retries", 1)
    result = build_worker_service(db_session).process_available_events()

    payment = db_session.get(Payment, payment_id)
    event = db_session.query(PaymentEvent).one()

    assert result.failed == 1
    assert payment.status == PaymentStatus.FAILED
    assert payment.retry_count == 1
    assert event.status == QueueMessageStatus.FAILED


def test_processing_endpoints_expose_metrics_and_event_state(client, db_session):
    success_id = create_payment(client, "dashboard-success")
    failure_id = create_payment(client, "dashboard-fail")

    build_worker_service(db_session).process_available_events(batch_size=10)

    metrics_response = client.get("/api/v1/payments/metrics")
    processing_response = client.get(f"/api/v1/payments/{failure_id}/processing")
    events_response = client.get(f"/api/v1/payments/{success_id}/events")

    assert metrics_response.status_code == 200
    assert metrics_response.json()["total"] == 2
    assert metrics_response.json()["completed"] == 1
    assert metrics_response.json()["failed"] == 1

    assert processing_response.status_code == 200
    assert processing_response.json()["status"] == "FAILED"
    assert processing_response.json()["latest_event_status"] == "FAILED"

    assert events_response.status_code == 200
    assert len(events_response.json()) == 1
    assert events_response.json()[0]["event_type"] == "payment.created"


def test_processing_endpoints_return_not_found_for_unknown_payment(client):
    payment_id = "84e8d21f-2547-494c-aaec-fd6a89fbe8f5"

    processing_response = client.get(f"/api/v1/payments/{payment_id}/processing")
    events_response = client.get(f"/api/v1/payments/{payment_id}/events")

    assert processing_response.status_code == 404
    assert events_response.status_code == 404
