"""Control B complete worker flows on disposable PostgreSQL, with a fake provider."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import socket
from threading import Event, Lock

import pytest
from sqlalchemy import select,text

from test_queue_concurrency import pg_engine
from test_postgres_stripe import migrated,owner,stripe_payment,service,payload,ACCOUNT
from test_stripe_leases import expire
from app.api.deps import build_worker_service
from app.models.payment import Payment,PaymentEvent,PaymentStatus,QueueMessageStatus
from app.queue import PostgresQueueConsumer
from app.repositories import SqlAlchemyPaymentRepository
from app.services.stripe_gateway import GatewayResult,GatewayStatus,PaymentGateway
from app.services.stripe_processor import StripePaymentProcessor
from app.services.worker_service import PaymentWorkerService


@pytest.fixture(autouse=True)
def no_network(pg_engine, monkeypatch):
    resolve = socket.getaddrinfo
    def forbidden(*a,**k):pytest.fail("Recovery test attempted external network")
    def test_database_only(host, port, *args, **kwargs):
        # psycopg resolves container hostnames through Python on some runtimes.
        # Allow only the already-validated disposable database; Stripe stays blocked.
        if pg_engine.url.host is not None and host == pg_engine.url.host and str(port) == str(pg_engine.url.port or 5432):
            return resolve(host, port, *args, **kwargs)
        return forbidden()
    monkeypatch.setattr(socket,"getaddrinfo",test_database_only)
    monkeypatch.setattr(socket.socket,"connect",forbidden)


class Provider(PaymentGateway):
    def __init__(self):
        self.intents={};self.requests=[];self.retrieves=[];self.lock=Lock()
        self.lost_response=False;self.hold_first=False;self.entered=Event();self.release=Event()
        self.session=None

    def create_payment(self,**params):
        if self.session is not None:assert not self.session.in_transaction()
        with self.lock:
            params.pop("request_deadline",None)
            self.requests.append(params.copy())
            key=params["idempotency_key"]
            if key not in self.intents:
                self.intents[key]=GatewayResult(GatewayStatus.SUCCEEDED,"pi_synthetic"+str(len(self.intents)),"succeeded",1200,1200,"eur",
                    tuple(sorted(params["metadata"].items())),account_id=ACCOUNT,livemode=False)
            result=self.intents[key];first=len(self.requests)==1
        if first and self.hold_first:
            self.entered.set();assert self.release.wait(10)
        if first and self.lost_response:raise TimeoutError("SYNTHETIC_PRIVATE_RESPONSE_LOST")
        return result

    def retrieve_payment(self,provider_reference):
        if self.session is not None:assert not self.session.in_transaction()
        self.retrieves.append(provider_reference)
        return next(result for result in self.intents.values() if result.provider_reference==provider_reference)


def seed(factory):
    with factory() as session:
        p=stripe_payment(session,owner(session))
        return p.id,p.merchant_id


def worker(session,gateway):
    return PaymentWorkerService(payment_repository=SqlAlchemyPaymentRepository(session),queue_consumer=PostgresQueueConsumer(session),
        stripe_processor=StripePaymentProcessor(gateway=gateway,request_budget_seconds=0),stripe_account_id=ACCOUNT)


def due(factory,payment_id):
    with factory.begin() as session:
        session.execute(text("UPDATE payments SET provider_next_attempt_at=clock_timestamp()-interval '1 second' WHERE id=:id"),{"id":payment_id})
        session.execute(text("UPDATE payment_events SET available_at=clock_timestamp()-interval '1 second' WHERE payment_id=:id"),{"id":payment_id})


def test_lost_response_then_new_worker_replays_one_provider_operation(migrated,pg_engine):
    payment_id,_=seed(migrated);gateway=Provider();gateway.lost_response=True
    with migrated() as session:
        gateway.session=session
        result=worker(session,gateway).process_available_events()
        assert result.unknown==1 and not session.in_transaction()
    gateway.session=None;pg_engine.dispose();due(migrated,payment_id)
    with migrated() as session:
        gateway.session=session
        assert worker(session,gateway).process_available_events().completed==1
        p=session.get(Payment,payment_id)
        assert p.status==PaymentStatus.COMPLETED and p.provider_reference=="pi_synthetic0"
    assert len(gateway.requests)==2 and gateway.requests[0]==gateway.requests[1]
    assert len(gateway.intents)==1


def test_concurrent_workers_create_one_intent(migrated):
    payment_id,_=seed(migrated);gateway=Provider()
    def run(_):
        with migrated() as session:return worker(session,gateway).process_available_events()
    with ThreadPoolExecutor(max_workers=6) as workers:results=list(workers.map(run,range(6)))
    assert sum(r.completed for r in results)==1
    assert len(gateway.requests)==len(gateway.intents)==1


def test_late_response_is_fenced_after_expired_lease_recovery(migrated):
    payment_id,_=seed(migrated);gateway=Provider();gateway.hold_first=True
    def run():
        with migrated() as session:return worker(session,gateway).process_available_events()
    with ThreadPoolExecutor(max_workers=2) as workers:
        late=workers.submit(run)
        assert gateway.entered.wait(5)
        try:
            expire(migrated,payment_id)
            current=workers.submit(run).result(timeout=5)
            assert current.completed==1
        finally:gateway.release.set()
        assert late.result(timeout=5).stale==1
    with migrated() as session:
        assert session.get(Payment,payment_id).status==PaymentStatus.COMPLETED
        assert session.get(Payment,payment_id).provider_attempt_count==2
    assert len(gateway.intents)==1 and gateway.requests[0]==gateway.requests[1]


def test_reference_reconciliation_uses_only_get(migrated):
    payment_id,_=seed(migrated);gateway=Provider()
    original=gateway.create_payment
    def pending(**params):
        result=original(**params)
        return replace(result,status=GatewayStatus.PENDING,provider_status="processing",amount_received=0)
    gateway.create_payment=pending
    with migrated() as session:assert worker(session,gateway).process_available_events().processing==1
    due(migrated,payment_id)
    with migrated() as session:assert worker(session,gateway).process_available_events().completed==1
    assert len(gateway.requests)==1 and gateway.retrieves==["pi_synthetic0"]


def test_expired_unknown_without_reference_never_posts(migrated):
    payment_id,_=seed(migrated);gateway=Provider()
    with migrated.begin() as session:
        first=datetime.now(UTC)-timedelta(hours=25)
        session.execute(text("UPDATE payments SET provider_state='unknown',status='PROCESSING',provider_first_attempt_at=:first,provider_retry_until=:last WHERE id=:id"),
                        {"first":first,"last":first+timedelta(hours=23),"id":payment_id})
    with migrated() as session:
        assert worker(session,gateway).process_available_events().review_required==1
        p=session.get(Payment,payment_id)
        assert p.status==PaymentStatus.PROCESSING and p.provider_state=="review_required"
        assert session.scalar(select(PaymentEvent.status))==QueueMessageStatus.FAILED
    assert gateway.requests==gateway.retrieves==[]


def test_duplicate_messages_are_completed_together(migrated):
    payment_id,_=seed(migrated);gateway=Provider()
    with migrated.begin() as session:
        event=session.scalar(select(PaymentEvent))
        session.add(PaymentEvent(payment_id=payment_id,queue_name=event.queue_name,event_type=event.event_type,payload={"untrusted":"ignored"}))
    with migrated() as session:
        assert worker(session,gateway).process_available_events().completed==1
        assert set(session.scalars(select(PaymentEvent.status)))=={QueueMessageStatus.COMPLETED}
    assert len(gateway.requests)==len(gateway.intents)==1


def test_default_worker_never_constructs_stripe_and_simulator_stays_functional(migrated,monkeypatch):
    payment_id,merchant_id=seed(migrated)
    def forbidden(*a,**k):pytest.fail("Default worker constructed Stripe")
    monkeypatch.setattr("app.api.deps.StripeGateway",forbidden)
    with migrated() as session:
        simulated=service(session).create_payment(payload("simulator-control-b"),merchant_id=merchant_id)
        result=build_worker_service(session).process_available_events()
        assert result.completed==1
        session.refresh(simulated)
        assert simulated.status==PaymentStatus.COMPLETED
        assert session.get(Payment,payment_id).provider_state=="prepared"


def test_crash_after_provider_success_before_database_result_is_recoverable(migrated,monkeypatch):
    payment_id,_=seed(migrated);gateway=Provider()
    with migrated() as session:
        instance=worker(session,gateway)
        def interrupted(*a,**k):raise RuntimeError("Synthetic result persistence interruption")
        monkeypatch.setattr(instance.payment_repository,"finish_provider_operation",interrupted)
        with pytest.raises(RuntimeError,match="Synthetic result persistence"):
            instance.process_available_events()
    expire(migrated,payment_id)
    with migrated() as session:
        assert worker(session,gateway).process_available_events().completed==1
    assert len(gateway.intents)==1 and gateway.requests[0]==gateway.requests[1]


def test_provider_wait_holds_no_payment_lock_or_session_transaction(migrated):
    payment_id,_=seed(migrated);gateway=Provider();original=gateway.create_payment
    with migrated() as session:
        def unlocked(**params):
            assert not session.in_transaction()
            with migrated.begin() as other:
                other.execute(text("SELECT id FROM payments WHERE id=:id FOR UPDATE NOWAIT"),{"id":payment_id})
            return original(**params)
        gateway.create_payment=unlocked
        assert worker(session,gateway).process_available_events().completed==1


def test_cross_merchant_provider_response_cannot_complete_other_payment(migrated):
    first_id,first_owner=seed(migrated)
    with migrated() as session:
        second=stripe_payment(session,owner(session,"Other owner"))
        second_id=second.id
    gateway=Provider();original=gateway.create_payment;first_result=[]
    def misrouted(**params):
        result=original(**params)
        if not first_result:first_result.append(result)
        return first_result[0]
    gateway.create_payment=misrouted
    with migrated() as session:
        result=worker(session,gateway).process_available_events()
        assert result.completed==1 and result.review_required==1
        assert session.get(Payment,first_id).status==PaymentStatus.COMPLETED
        assert session.get(Payment,second_id).provider_state=="review_required"
        assert session.get(Payment,second_id).provider_reference is None



def test_late_duplicate_is_acknowledged_without_another_provider_call(migrated):
    payment_id,_=seed(migrated);gateway=Provider()
    with migrated() as session:
        assert worker(session,gateway).process_available_events().completed==1
        event=session.scalar(select(PaymentEvent))
        session.add(PaymentEvent(payment_id=payment_id,queue_name=event.queue_name,event_type=event.event_type,payload={}))
        session.commit()
        assert worker(session,gateway).process_available_events().claimed==0
        assert set(session.scalars(select(PaymentEvent.status)))=={QueueMessageStatus.COMPLETED}
    assert len(gateway.requests)==len(gateway.intents)==1



def test_conflicting_provider_reference_stops_in_review_without_new_charge(migrated):
    first_id,_=seed(migrated)
    with migrated() as session:
        second=stripe_payment(session,owner(session,"Other owner"));second_id=second.id
    gateway=Provider();original=gateway.create_payment
    def conflicting(**params):return replace(original(**params),provider_reference="pi_conflicting")
    gateway.create_payment=conflicting
    with migrated() as session:
        result=worker(session,gateway).process_available_events()
        assert result.completed==1 and result.review_required==1
        assert session.get(Payment,first_id).provider_reference=="pi_conflicting"
        p=session.get(Payment,second_id)
        assert p.provider_reference is None and p.provider_state=="review_required"
        assert p.failure_reason=="provider_reference_conflict"
        session.commit()
        assert worker(session,gateway).process_available_events().claimed==0
    assert len(gateway.requests)==2


def test_explicit_factory_activation_uses_only_synthetic_server_key(migrated,monkeypatch):
    from pydantic import SecretStr
    from app.core.config import settings
    payment_id,_=seed(migrated);gateway=Provider();configured=[]
    def build_gateway(**kwargs):configured.append(kwargs);return gateway
    monkeypatch.setattr(settings,"stripe_worker_enabled",True)
    monkeypatch.setattr(settings,"stripe_account_id",ACCOUNT)
    monkeypatch.setattr(settings,"stripe_secret_key",SecretStr("sk_test_SYNTHETIC_FACTORY_ONLY"))
    monkeypatch.setattr(settings,"stripe_secret_key_file",None)
    monkeypatch.setattr("app.api.deps.StripeGateway",build_gateway)
    with migrated() as session:
        assert build_worker_service(session).process_available_events().completed==1
    assert configured[0]["account_id"]==ACCOUNT
