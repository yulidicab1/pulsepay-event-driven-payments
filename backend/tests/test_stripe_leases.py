"""Control A: PostgreSQL leases/fencing before connecting any Stripe processor."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from alembic import op
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from test_queue_concurrency import pg_engine
from test_postgres_stripe import migrated, owner, stripe_payment, payload, service, ACCOUNT
from test_postgres_auth import migrate
from app.models.payment import Payment, PaymentEvent, PaymentStatus, QueueMessageStatus
from app.repositories import SqlAlchemyPaymentRepository


def seed(factory, duplicates=0):
    with factory() as session:
        p = stripe_payment(session, owner(session))
        for _ in range(duplicates):
            event = session.scalar(select(PaymentEvent).where(PaymentEvent.payment_id == p.id))
            session.add(PaymentEvent(payment_id=p.id, queue_name=event.queue_name,
                event_type=event.event_type, payload={"merchant_id": str(uuid4())}))
        session.commit()
        return p.id, p.merchant_id


def claim(factory, **kwargs):
    with factory() as session:
        result = SqlAlchemyPaymentRepository(session).claim_provider_operation(account_id=ACCOUNT, lease_seconds=120, **kwargs)
        assert not session.in_transaction()
        return result


def expire(factory, payment_id):
    with factory.begin() as session:
        session.execute(text("UPDATE payments SET provider_lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": payment_id})


def finish(factory, operation, **kwargs):
    with factory() as session:
        result = SqlAlchemyPaymentRepository(session).finish_provider_operation(operation, **kwargs)
        assert not session.in_transaction()
        return result


def test_concurrent_workers_and_duplicate_events_share_one_lease(migrated):
    payment_id, _ = seed(migrated, duplicates=3)
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(lambda _: claim(migrated), range(6)))
    claims = [item for item in results if item is not None]
    assert len(claims) == 1 and claims[0].payment_id == payment_id
    with migrated() as session:
        p = session.get(Payment, payment_id)
        assert p.provider_attempt_count == 1
        assert session.scalar(select(func.count(PaymentEvent.id))) == 4


def test_restart_recovers_expired_claim_with_identical_contract(migrated, pg_engine):
    payment_id, _ = seed(migrated)
    first = claim(migrated)
    expire(migrated, payment_id)
    pg_engine.dispose()
    second = claim(migrated)
    assert second.execution_token != first.execution_token and second.attempt_count == 2
    assert (second.request, second.request_hash, second.idempotency_key, second.retry_until) == (first.request, first.request_hash, first.idempotency_key, first.retry_until)
    assert finish(migrated, first, state="succeeded", provider_status="succeeded", provider_reference="pi_stale") is False
    assert finish(migrated, second, state="succeeded", provider_status="succeeded", provider_reference="pi_current") is True
    with migrated() as session:
        p = session.get(Payment, payment_id)
        assert p.status == PaymentStatus.COMPLETED and p.provider_reference == "pi_current"


def test_expired_worker_cannot_apply_even_before_reclaim(migrated):
    payment_id, _ = seed(migrated)
    operation = claim(migrated)
    expire(migrated, payment_id)
    assert not finish(migrated, operation, state="succeeded", provider_status="succeeded", provider_reference="pi_late")
    with migrated() as session:
        assert session.get(Payment, payment_id).provider_reference is None


def test_wrong_merchant_account_token_cannot_finish_operation(migrated):
    seed(migrated)
    operation = claim(migrated)
    for bad in [replace(operation, merchant_id=uuid4()), replace(operation, account_id="acct_other"), replace(operation, execution_token=uuid4())]:
        assert not finish(migrated, bad, state="unknown", reason="synthetic_error")
    assert finish(migrated, operation, state="unknown", reason="provider_uncertain")


def test_foreign_account_and_owner_are_not_claimed(migrated):
    payment_id, merchant_id = seed(migrated)
    assert claim(migrated, merchant_id=uuid4()) is None
    with migrated() as session:
        repo = SqlAlchemyPaymentRepository(session)
        assert repo.claim_provider_operation(account_id="acct_other", lease_seconds=120) is None
    assert claim(migrated, merchant_id=merchant_id).payment_id == payment_id


def test_terminal_result_is_frozen_and_duplicate_completion_is_ignored(migrated):
    payment_id, _ = seed(migrated, duplicates=2)
    operation = claim(migrated)
    assert finish(migrated, operation, state="succeeded", provider_status="succeeded", provider_reference="pi_final")
    assert not finish(migrated, operation, state="unknown")
    assert claim(migrated) is None
    with migrated() as session:
        assert set(session.scalars(select(PaymentEvent.status))) == {QueueMessageStatus.COMPLETED}
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE payments SET provider_state='unknown',status='PROCESSING' WHERE id=:id"), {"id": payment_id})
            session.commit()
        session.rollback()


def test_retry_schedule_and_known_reference_survive_reclaim(migrated):
    payment_id, _ = seed(migrated)
    first = claim(migrated)
    assert finish(migrated, first, state="pending", provider_status="processing", provider_reference="pi_pending")
    assert claim(migrated) is None
    with migrated.begin() as session:
        session.execute(text("UPDATE payments SET provider_next_attempt_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": payment_id})
        session.execute(text("UPDATE payment_events SET available_at=clock_timestamp()-interval '1 second' WHERE payment_id=:id"), {"id": payment_id})
    second = claim(migrated)
    assert second.provider_reference == "pi_pending" and second.idempotency_key == first.idempotency_key


def test_claim_and_result_refuse_to_commit_unrelated_open_transaction(migrated):
    seed(migrated)
    with migrated() as session:
        session.execute(select(func.now()))
        with pytest.raises(RuntimeError, match="idle transaction"):
            SqlAlchemyPaymentRepository(session).claim_provider_operation(account_id=ACCOUNT, lease_seconds=120)
        session.rollback()


def test_interrupted_lease_migration_rolls_back_and_retries(pg_engine, monkeypatch):
    from app.db.session import Base
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "s001_stripe_payment_operations")
    original = op.execute
    def interrupted(statement, *args, **kwargs):
        if "CREATE OR REPLACE FUNCTION" in str(statement): raise RuntimeError("Synthetic lease interruption")
        return original(statement, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(op, "execute", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic lease interruption"):
            migrate(pg_engine, "head")
    with pg_engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "s001_stripe_payment_operations"
        assert c.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='payments' AND column_name='provider_execution_token'")) == 0
    migrate(pg_engine, "head")
    with pg_engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "s002_stripe_execution_leases"



def test_legacy_null_backend_cannot_acquire_lease_fields(pg_engine):
    from app.db.session import Base
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine,"head")
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO payments(id,merchant_reference,amount,currency,customer_email,status,retry_count) VALUES(:id,'legacy-no-lease',12,'EUR','synthetic@example.com','PENDING',0)"),{"id":uuid4()})
    with pytest.raises(IntegrityError):
        with pg_engine.begin() as c:
            c.execute(text("UPDATE payments SET provider_execution_token=:token,provider_lease_until=clock_timestamp()+interval '1 minute'"),{"token":uuid4()})


def test_downgrade_refuses_to_erase_lease_evidence(migrated,pg_engine):
    from alembic import command
    from alembic.config import Config
    seed(migrated);operation=claim(migrated)
    backend=Path(__file__).resolve().parents[1]
    config=Config(str(backend/"alembic.ini"))
    config.set_main_option("script_location",str(backend/"migrations"))
    with pytest.raises(RuntimeError,match="recovery plan"):
        with pg_engine.begin() as c:
            config.attributes["connection"]=c
            command.downgrade(config,"s001_stripe_payment_operations")
    with migrated() as session:
        assert session.get(Payment,operation.payment_id).provider_execution_token==operation.execution_token
