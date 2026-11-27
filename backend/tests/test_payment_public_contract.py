"""Public provider contract and note redaction; synthetic values only."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.api.deps import build_payment_service
from app.models.payment import Payment, PaymentEvent, PaymentStatus
from app.schemas.payment import PaymentCreate
from app.schemas.provider import PUBLIC_NOTES, public_processing_note

PRIVATE = "SYNTHETIC_PRIVATE_INTERNAL_VALUE"


def prepare(session, merchant_id):
    return build_payment_service(session).create_payment(
        PaymentCreate(merchant_reference="public-provider-contract",amount="12.00",currency="EUR",customer_email="synthetic@example.com"),
        merchant_id=merchant_id,processing_backend="stripe",provider_account_id="acct_syntheticPublic",payment_method="pm_card_visa")


@pytest.mark.parametrize("state,raw_status", [
    ("prepared",None),("in_flight",None),("pending","processing"),
    ("requires_action","requires_action"),("unknown",None),("review_required",None),
    ("succeeded","succeeded"),("failed","canceled"),
])
def test_public_state_is_distinct_from_payment_outcome(client,db_session,merchant,state,raw_status):
    p=prepare(db_session,merchant.id)
    if state != "prepared":
        p.provider_state=state;p.provider_status=raw_status
        p.provider_first_attempt_at=datetime.now(UTC);p.provider_retry_until=p.provider_first_attempt_at+timedelta(hours=23)
        p.status={"succeeded":PaymentStatus.COMPLETED,"failed":PaymentStatus.FAILED}.get(state,PaymentStatus.PROCESSING)
        if state in {"succeeded","failed","pending","requires_action"}:p.provider_reference="pi_syntheticPublic"
        if state=="in_flight":p.provider_execution_token=uuid4();p.provider_lease_until=datetime.now(UTC)+timedelta(minutes=2)
    db_session.commit()
    for path in [f"/{p.id}",f"/{p.id}/processing",""]:
        response=client.get("/api/v1/payments"+path)
        assert response.status_code==200
        data=response.json()[0] if path=="" else response.json()
        assert data["provider_state"]==state and data["provider_status"]==raw_status
        assert data["provider_reference"]==p.provider_reference
        if state in {"unknown","review_required"}:assert data["status"]=="PROCESSING"
        assert "provider_request" not in data and "provider_idempotency_key" not in data
        assert all(value not in response.text for value in [p.provider_request_hash,p.provider_idempotency_key,"acct_syntheticPublic",str(p.provider_execution_token) if p.provider_execution_token else PRIVATE])


def test_private_notes_are_redacted_in_all_public_endpoints(client,db_session,merchant):
    p=prepare(db_session,merchant.id);p.failure_reason=PRIVATE
    event=db_session.scalar(select(PaymentEvent).where(PaymentEvent.payment_id==p.id));event.last_error=PRIVATE
    db_session.commit()
    for suffix in ["",f"/{p.id}",f"/{p.id}/processing",f"/{p.id}/events"]:
        response=client.get("/api/v1/payments"+suffix)
        assert response.status_code==200 and PRIVATE not in response.text
    assert db_session.get(Payment,p.id).failure_reason==PRIVATE


def test_historical_reference_is_preserved_but_not_disclosed_as_stripe(client,db_session,merchant):
    response=client.post("/api/v1/payments",json={"merchant_reference":"historical-private-ref","amount":"12.00","currency":"EUR","customer_email":"synthetic@example.com"})
    from uuid import UUID
    payment_id=UUID(response.json()["id"])
    db_session.execute(update(Payment).where(Payment.id==payment_id).values(processing_backend=None,provider_reference=PRIVATE));db_session.commit();db_session.expire_all()
    result=client.get(f"/api/v1/payments/{payment_id}")
    assert result.status_code==200 and result.json()["processing_backend"] is None
    assert result.json()["provider_reference"] is None and PRIVATE not in result.text
    assert db_session.get(Payment,payment_id).provider_reference==PRIVATE


@pytest.mark.parametrize("code",list(PUBLIC_NOTES))
def test_public_notes_are_allowlisted_and_idempotent(code):
    value=public_processing_note(code)
    assert value==PUBLIC_NOTES[code] and public_processing_note(value)==value


def test_frontend_cannot_choose_provider_account_or_owner(client,db_session):
    response=client.post("/api/v1/payments",json={"merchant_reference":"untrusted-provider-fields","amount":"12.00","currency":"EUR","customer_email":"synthetic@example.com",
        "processing_backend":"stripe","provider_account_id":"acct_syntheticAttack","provider_reference":"pi_attack","merchant_id":str(uuid4())})
    assert response.status_code==201 and response.json()["processing_backend"]=="simulator"
    assert response.json()["provider_reference"] is None
