"""Durable Stripe contracts on disposable PostgreSQL; no provider client or I/O."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4

from alembic import command, op
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import DropSchema

from test_queue_concurrency import pg_engine
from test_postgres_auth import migrate
from app.api.deps import build_worker_service, get_db_session
from app.core.config import settings
from app.db.session import Base
from app.main import app
from app.models.payment import IdempotencyKey, Merchant, Payment, PaymentEvent, PaymentStatus
from app.queue import PostgresQueuePublisher
from app.repositories import SqlAlchemyMerchantRepository, SqlAlchemyPaymentRepository
from app.repositories.auth_repository import SqlAlchemyAuthRepository
from app.schemas.payment import PaymentCreate
from app.services.auth_service import AuthService
from app.services.merchant_service import MerchantService
from app.services.payment_service import PaymentService
from app.services.stripe_gateway import StripeGateway

ACCOUNT = "acct_syntheticA"
PASSWORD = "synthetic-operation-password"


@pytest.fixture(autouse=True)
def no_gateway_and_cleanup(pg_engine, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Activity 2 attempted to construct a Stripe gateway")
    monkeypatch.setattr(StripeGateway, "__init__", forbidden)
    yield
    with pg_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def migrated(pg_engine):
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "head")
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


def owner(session, name="Synthetic owner"):
    merchant, _ = MerchantService(repository=SqlAlchemyMerchantRepository(session)).create_merchant(name, issue_api_key=False)
    return merchant.id


def service(session, publisher=None):
    return PaymentService(repository=SqlAlchemyPaymentRepository(session),
                          publisher=publisher if publisher is not None else PostgresQueuePublisher(session))


def payload(reference="stripe-durable-order"):
    return PaymentCreate(merchant_reference=reference, amount=Decimal("12.00"), currency="EUR",
                         customer_email="synthetic@example.com", description="synthetic private description")


def stripe_payment(session, merchant_id, reference="stripe-durable-order", key="client-key", account=ACCOUNT):
    return service(session).create_payment(payload(reference), merchant_id=merchant_id,
        idempotency_key=key, processing_backend="stripe", provider_account_id=account, payment_method="pm_card_visa")


def start_reconciliation(session, payment, reference=None):
    payment.provider_state = "unknown"
    payment.provider_first_attempt_at = datetime.now(UTC)
    payment.provider_retry_until = payment.provider_first_attempt_at + timedelta(hours=23)
    payment.status = PaymentStatus.PROCESSING
    payment.provider_reference = reference
    session.commit()


def test_contract_is_durable_and_client_key_is_separate(migrated, pg_engine):
    with migrated() as session:
        merchant_id = owner(session)
        payment = stripe_payment(session, merchant_id)
        payment_id = payment.id
        expected = (payment.provider_idempotency_key, payment.provider_request, payment.provider_request_hash)
        request = json.loads(payment.provider_request)
        assert request["account_id"] == ACCOUNT and request["livemode"] is False
        assert request["params"]["metadata"] == {"merchant_id": str(merchant_id), "payment_id": str(payment.id)}
        assert request["params"]["amount"] == 1200
        assert hashlib.sha256(payment.provider_request.encode()).hexdigest() == payment.provider_request_hash
        assert "synthetic@example.com" not in payment.provider_request and "private description" not in payment.provider_request
        assert payment.provider_idempotency_key != "client-key"
        assert session.scalar(select(IdempotencyKey).where(IdempotencyKey.payment_id == payment_id)).key == "client-key"
        assert session.scalar(select(func.count(PaymentEvent.id))) == 1
        assert payment.provider_state == "prepared" and payment.provider_first_attempt_at is None
    pg_engine.dispose()
    with migrated() as session:
        recovered = SqlAlchemyPaymentRepository(session).get_provider_operation(payment_id, merchant_id, account_id=ACCOUNT)
        assert (recovered.provider_idempotency_key, recovered.provider_request, recovered.provider_request_hash) == expected
    # A genuinely new Python process reads the committed operation, not an ORM cache.
    script = """
import json,os
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.repositories import SqlAlchemyPaymentRepository
engine=create_engine(os.environ['TEST_POSTGRES_URL'],hide_parameters=True,connect_args={'options':'-csearch_path='+os.environ['TEST_SCHEMA']})
with Session(engine) as session:
 p=SqlAlchemyPaymentRepository(session).get_provider_operation(UUID(os.environ['PAYMENT_ID']),UUID(os.environ['MERCHANT_ID']),account_id='acct_syntheticA')
 print(json.dumps([p.provider_idempotency_key,p.provider_request,p.provider_request_hash]))
"""
    env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "TEST_POSTGRES_URL") if k in os.environ}
    with pg_engine.connect() as connection:
        schema = connection.scalar(select(func.current_schema()))
    env.update(DATABASE_URL="sqlite://", APP_ENV="test", PUBLIC_ORIGIN="http://localhost:5173",
               PYTHONDONTWRITEBYTECODE="1", TEST_SCHEMA=schema, PAYMENT_ID=str(payment_id), MERCHANT_ID=str(merchant_id))
    import sys
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert tuple(json.loads(result.stdout)) == expected


def test_http_replay_preserves_original_context_after_server_change(migrated):
    with migrated() as session:
        merchant_id = owner(session)
        first = stripe_payment(session, merchant_id)
        replay = service(session).create_payment(payload(), merchant_id=merchant_id, idempotency_key="client-key",
            processing_backend="stripe", provider_account_id="acct_syntheticChanged", payment_method="pm_other")
        assert replay.id == first.id and replay.provider_account_id == ACCOUNT
        assert replay.provider_request == first.provider_request and replay.provider_idempotency_key == first.provider_idempotency_key
        assert session.scalar(select(func.count(Payment.id))) == session.scalar(select(func.count(PaymentEvent.id))) == 1


def test_concurrent_replays_commit_one_operation_and_event(migrated):
    with migrated() as session:
        merchant_id = owner(session)
    def attempt(_):
        with migrated() as session:
            p = stripe_payment(session, merchant_id)
            return p.id, p.provider_idempotency_key, p.provider_request_hash
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert len(set(results)) == 1
    with migrated() as session:
        assert session.scalar(select(func.count(Payment.id))) == 1
        assert session.scalar(select(func.count(PaymentEvent.id))) == 1
        assert session.scalar(select(func.count(IdempotencyKey.id))) == 1


def test_repository_scope_and_same_client_key_across_merchants(migrated):
    with migrated() as session:
        one, two = owner(session, "One"), owner(session, "Two")
        a, b = stripe_payment(session, one), stripe_payment(session, two)
        repo = SqlAlchemyPaymentRepository(session)
        assert a.provider_idempotency_key != b.provider_idempotency_key
        assert repo.get_provider_operation(a.id, two, account_id=ACCOUNT) is None
        assert repo.get_provider_operation(a.id, one, account_id="acct_other") is None
        assert repo.get_provider_operation(a.id, one, account_id=ACCOUNT, for_update=True).id == a.id
        assert repo.get(a.id, two) is None


@pytest.mark.parametrize("column,value", [
    ("amount", Decimal("13.00")), ("currency", "USD"), ("merchant_reference", "changed-reference"),
    ("provider_account_id", "acct_changed"), ("provider_livemode", True),
    ("provider_idempotency_key", "different-provider-key"), ("provider_request", "{}"),
    ("provider_request_hash", "0" * 64), ("processing_backend", "simulator"), ("processing_backend", None),
])
def test_sql_cannot_mutate_durable_identity_or_parameters(migrated, column, value):
    with migrated() as session:
        payment = stripe_payment(session, owner(session))
        payment_id = payment.id
        with pytest.raises(IntegrityError):
            session.execute(text(f"UPDATE payments SET {column}=:value WHERE id=:id"), {"value": value, "id": payment_id})
            session.commit()
        session.rollback()
        assert session.get(Payment, payment_id).provider_state == "prepared"


def test_self_consistent_rewrite_and_owner_swap_are_blocked(migrated):
    with migrated() as session:
        one, two = owner(session, "One"), owner(session, "Two")
        payment = stripe_payment(session, one)
        request = json.loads(payment.provider_request)
        request["account_id"] = "acct_other"
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE payments SET provider_account_id='acct_other',provider_request=:request,provider_request_hash=:hash WHERE id=:id"),
                            {"request": encoded, "hash": hashlib.sha256(encoded.encode()).hexdigest(), "id": payment.id})
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE payments SET merchant_id=:owner WHERE id=:id"), {"owner": two, "id": payment.id})
            session.commit()
        session.rollback()


def test_provider_references_unique_per_account_and_not_reassignable(migrated):
    with migrated() as session:
        one, two = owner(session, "One"), owner(session, "Two")
        a, b = stripe_payment(session, one), stripe_payment(session, two)
        start_reconciliation(session, a, "pi_sharedSynthetic")
        with pytest.raises(IntegrityError) as failure:
            start_reconciliation(session, b, "pi_sharedSynthetic")
        assert failure.value.orig.diag.constraint_name == "uq_payment_provider_reference"
        session.rollback()
        for value in (None, "pi_replaced"):
            with pytest.raises(IntegrityError):
                a.provider_reference = value
                session.commit()
            session.rollback()


def test_retry_deadline_and_stripe_record_cannot_be_erased(migrated):
    with migrated() as session:
        payment = stripe_payment(session, owner(session))
        start_reconciliation(session, payment, "pi_recoveredSynthetic")
        for statement in ("UPDATE payments SET provider_retry_until=provider_retry_until+interval '1 hour' WHERE id=:id",
                          "UPDATE payments SET provider_first_attempt_at=NULL,provider_retry_until=NULL WHERE id=:id",
                          "DELETE FROM payments WHERE id=:id"):
            with pytest.raises(IntegrityError):
                session.execute(text(statement), {"id": payment.id})
                session.commit()
            session.rollback()


@pytest.mark.parametrize("mutation", ["hash", "account", "owner", "key", "live", "extra", "deadline"])
def test_postgres_rejects_invalid_contracts_on_insert(migrated, mutation):
    with migrated() as session:
        merchant_id = owner(session)
        p = Payment(id=uuid4(), merchant_id=merchant_id, merchant_reference="invalid-contract", amount=Decimal("12.00"),
                    currency="EUR", customer_email="synthetic@example.com", status=PaymentStatus.PENDING)
        PaymentService._prepare_processing(p, "stripe", ACCOUNT, "pm_card_visa")
        if mutation == "hash":p.provider_request_hash = "0" * 64
        elif mutation == "account":p.provider_account_id = "acct_wrong"
        elif mutation == "key":p.provider_idempotency_key = "client-key"
        elif mutation == "live":p.provider_livemode = True
        elif mutation in {"owner", "extra"}:
            request = json.loads(p.provider_request)
            if mutation == "owner":request["params"]["metadata"]["merchant_id"] = str(uuid4())
            else:request["params"]["client_secret"] = "synthetic-not-allowed"
            p.provider_request = json.dumps(request, sort_keys=True, separators=(",", ":"))
            p.provider_request_hash = hashlib.sha256(p.provider_request.encode()).hexdigest()
        else:
            p.provider_state = "unknown"
            p.provider_first_attempt_at = datetime.now(UTC)
            p.provider_retry_until = p.provider_first_attempt_at + timedelta(hours=25)
        session.add(p)
        with pytest.raises(IntegrityError):session.commit()
        session.rollback()
        assert session.scalar(select(func.count(Payment.id))) == 0


def test_database_key_constraint_exists_and_duplicate_cannot_persist(migrated, pg_engine):
    uniques = {c["name"]: c["column_names"] for c in inspect(pg_engine).get_unique_constraints("payments")}
    assert uniques["uq_payment_provider_key"] == ["provider_account_id", "provider_livemode", "provider_idempotency_key"]
    with migrated() as session:
        merchant_id = owner(session)
        first = stripe_payment(session, merchant_id)
        second = Payment(id=uuid4(), merchant_id=merchant_id, merchant_reference="other-operation", amount=first.amount,
                         currency=first.currency, customer_email="synthetic@example.com", status=PaymentStatus.PENDING)
        PaymentService._prepare_processing(second, "stripe", ACCOUNT, "pm_card_visa")
        second.provider_idempotency_key = first.provider_idempotency_key
        session.add(second)
        with pytest.raises(IntegrityError):session.commit()
        session.rollback()
        assert session.scalar(select(func.count(Payment.id))) == 1


def test_atomic_payment_contract_and_outbox_on_failure(migrated):
    class BrokenPublisher:
        def publish(self, envelope):raise RuntimeError("Synthetic outbox failure")
    with migrated() as session:
        merchant_id = owner(session)
        with pytest.raises(RuntimeError, match="Synthetic outbox"):
            service(session, BrokenPublisher()).create_payment(payload(), merchant_id=merchant_id,
                idempotency_key="client-key", processing_backend="stripe", provider_account_id=ACCOUNT, payment_method="pm_card_visa")
        # Even a caller committing after catching the failure cannot persist a partial operation.
        session.commit()
        assert session.scalar(select(func.count(Payment.id))) == 0
        assert session.scalar(select(func.count(PaymentEvent.id))) == 0
        assert session.scalar(select(func.count(IdempotencyKey.id))) == 0


def test_simulator_works_and_never_claims_prepared_stripe(migrated):
    with migrated() as session:
        merchant_id = owner(session)
        prepared = stripe_payment(session, merchant_id)
        simulated = service(session).create_payment(payload("simulator-order"), merchant_id=merchant_id)
        result = build_worker_service(session).process_available_events()
        assert result.claimed == result.completed == 1
        assert simulated.status == PaymentStatus.COMPLETED
        session.refresh(prepared)
        assert prepared.status == PaymentStatus.PENDING and prepared.provider_state == "prepared"
        assert prepared.provider_reference is None and prepared.provider_first_attempt_at is None


def test_api_auth_scope_and_untrusted_provider_fields(migrated):
    with migrated() as session:
        one, two = owner(session, "One"), owner(session, "Two")
        a, b = stripe_payment(session, one), stripe_payment(session, two)
        AuthService(SqlAlchemyAuthRepository(session), settings).create_user(one, "synthetic-owner@example.com", PASSWORD)
        def database():yield session
        app.dependency_overrides[get_db_session] = database
        try:
            with TestClient(app) as client:
                csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
                logged = client.post("/api/v1/auth/login", json={"email": "synthetic-owner@example.com", "password": PASSWORD},
                    headers={"Origin": settings.public_origin, "X-CSRF-Token": csrf})
                assert logged.status_code == 200
                for suffix in ("", "/events", "/processing"):
                    assert client.get(f"/api/v1/payments/{b.id}{suffix}").status_code == 404
                own = client.get(f"/api/v1/payments/{a.id}")
                assert own.status_code == 200
                assert all(value not in own.text for value in (a.provider_idempotency_key, a.provider_request_hash, ACCOUNT))
                data = json.loads(payload("untrusted-provider").model_dump_json())
                data.update(processing_backend="stripe", merchant_id=str(two), provider_account_id="acct_attack", provider_request="untrusted")
                headers = {"Origin": settings.public_origin, "X-CSRF-Token": logged.json()["csrf_token"]}
                assert client.post("/api/v1/payments", json=data).status_code == 403
                created = client.post("/api/v1/payments", json=data, headers=headers)
                assert created.status_code == 201
                from uuid import UUID
                stored = session.get(Payment, UUID(created.json()["id"]))
                assert stored.merchant_id == one and stored.processing_backend == "simulator"
                assert stored.provider_request is None and stored.provider_account_id is None
                assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
                assert client.get(f"/api/v1/payments/{a.id}").status_code == 401
        finally:app.dependency_overrides.clear()


@pytest.mark.parametrize("preexisting_reference", [False, True])
def test_additive_migration_preserves_owned_and_unowned_history(pg_engine, preexisting_reference):
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "a001_dashboard_sessions")
    merchant_id, owned_id, unowned_id = uuid4(), uuid4(), uuid4()
    with pg_engine.begin() as c:
        if preexisting_reference:
            c.execute(text("ALTER TABLE payments ADD COLUMN provider_reference varchar(255)"))
            c.execute(text("CREATE INDEX ix_payments_provider_reference ON payments(provider_reference)"))
        c.execute(text("INSERT INTO merchants(id,name) VALUES(:id,'Historical owner')"), {"id": merchant_id})
        for payment_id, owner_id, ref in [(owned_id, merchant_id, "owned-history"), (unowned_id, None, "unowned-history")]:
            c.execute(text("INSERT INTO payments(id,merchant_id,merchant_reference,amount,currency,customer_email,status,retry_count) VALUES(:id,:owner,:ref,42,'EUR','history@example.com','PENDING',2)"),
                      {"id": payment_id, "owner": owner_id, "ref": ref})
            c.execute(text("INSERT INTO payment_events(id,payment_id,queue_name,event_type,payload,status,attempts) VALUES(:id,:payment,:queue,'payment.created','{}','PENDING',0)"),
                      {"id": uuid4(), "payment": payment_id, "queue": settings.payment_queue_name})
            c.execute(text("INSERT INTO idempotency_keys(id,merchant_id,key,request_hash,payment_id) VALUES(:id,:owner,:key,'synthetic-hash',:payment)"),
                      {"id": uuid4(), "owner": owner_id, "key": ref, "payment": payment_id})
        if preexisting_reference:
            c.execute(text("UPDATE payments SET provider_reference='pi_legacySynthetic' WHERE id=:id"), {"id": owned_id})
        before = c.execute(text("SELECT id,merchant_id,merchant_reference,amount,currency,status,retry_count,created_at,updated_at FROM payments ORDER BY id")).all()
    migrate(pg_engine, "head")
    with pg_engine.connect() as c:
        assert c.execute(text("SELECT id,merchant_id,merchant_reference,amount,currency,status,retry_count,created_at,updated_at FROM payments ORDER BY id")).all() == before
        assert c.scalar(text("SELECT count(*) FROM payments WHERE processing_backend IS NULL")) == 2
        if preexisting_reference:
            assert c.scalar(text("SELECT provider_reference FROM payments WHERE id=:id"), {"id": owned_id}) == "pi_legacySynthetic"
    with sessionmaker(bind=pg_engine)() as session:
        assert build_worker_service(session).process_available_events().claimed == 0
        assert session.scalar(select(func.sum(PaymentEvent.attempts))) == 0


def test_interrupted_provider_upgrade_rolls_back_and_recovers(pg_engine, monkeypatch):
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "a001_dashboard_sessions")
    payment_id = uuid4()
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO payments(id,merchant_reference,amount,currency,customer_email,status,retry_count) VALUES(:id,'interrupted-provider',12,'EUR','history@example.com','PENDING',0)"), {"id": payment_id})
    execute = op.execute
    def interrupted(statement, *args, **kwargs):
        if "CREATE TRIGGER" in str(statement):raise RuntimeError("Synthetic provider interruption")
        return execute(statement, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(op, "execute", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic provider interruption"):migrate(pg_engine, "head")
    with pg_engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "a001_dashboard_sessions"
        assert c.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='payments' AND column_name='processing_backend'")) == 0
        assert c.scalar(text("SELECT to_regprocedure('pulsepay_guard_payment_operation()')")) is None
        assert c.scalar(text("SELECT merchant_reference FROM payments WHERE id=:id"), {"id": payment_id}) == "interrupted-provider"
    migrate(pg_engine, "head")
    with pg_engine.connect() as c:
        assert c.scalar(text("SELECT processing_backend FROM payments WHERE id=:id"), {"id": payment_id}) is None


def test_provider_downgrade_refuses_evidence_loss(migrated, pg_engine):
    with migrated() as session:
        payment = stripe_payment(session, owner(session))
        expected = (payment.provider_request, payment.provider_idempotency_key)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with pytest.raises(RuntimeError, match="recovery plan"):
        with pg_engine.begin() as c:
            config.attributes["connection"] = c
            command.downgrade(config, "a001_dashboard_sessions")
    with migrated() as session:
        stored = session.get(Payment, payment.id)
        assert (stored.provider_request, stored.provider_idempotency_key) == expected
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "s001_stripe_payment_operations"


def test_backup_restore_preserves_contract_and_database_guards(migrated, pg_engine, tmp_path):
    raw = os.getenv("TEST_BACKUP_POSTGRES_URL")
    if not raw:pytest.skip("Set TEST_BACKUP_POSTGRES_URL for the independent restore test")
    target_url = make_url(raw)
    assert target_url.drivername in {"postgresql", "postgresql+psycopg"} and target_url.database.endswith("_test")
    assert target_url.database != pg_engine.url.database
    binary = Path(os.environ["TEST_POSTGRES_BIN"])
    with migrated() as session:
        payment = stripe_payment(session, owner(session))
        start_reconciliation(session, payment, "pi_backupSynthetic")
        expected = (payment.merchant_id, payment.provider_idempotency_key, payment.provider_request, payment.provider_request_hash, payment.provider_reference, payment.provider_state, payment.provider_first_attempt_at, payment.provider_retry_until)
        schema = session.scalar(select(func.current_schema()))
    assert re.fullmatch(r"pulsepay_test_[a-f0-9]{32}", schema)
    def environment(url):
        values = {"PATH": os.environ["PATH"], "PGHOST": str(url.query.get("host", url.host or "")),
                  "PGPORT": str(url.query.get("port", url.port or 5432)), "PGUSER": url.username or "",
                  "PGDATABASE": url.database, "PGPASSFILE": str(tmp_path / "unused-password")}
        if url.password:values["PGPASSWORD"] = url.password
        return values
    backup = tmp_path / "synthetic-provider.dump"
    result = subprocess.run([str(binary/'pg_dump'), '--no-owner', '--no-privileges', '--format=custom', '--schema', schema, '--file', str(backup)],
                            env=environment(pg_engine.url), capture_output=True, timeout=20)
    assert result.returncode == 0
    target = create_engine(target_url.set(drivername="postgresql+psycopg"), hide_parameters=True, connect_args={"connect_timeout": 3})
    attempted = False
    try:
        with target.connect() as c:
            assert c.scalar(select(func.current_database())) == target_url.database
            assert not c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:name)"), {"name": schema})
        attempted = True
        result = subprocess.run([str(binary/'pg_restore'), '--no-owner', '--no-privileges', '--exit-on-error', '--dbname', target_url.database, str(backup)],
                                env=environment(target_url), capture_output=True, timeout=20)
        assert result.returncode == 0
        with target.connect() as c:
            row = c.execute(text(f'SELECT merchant_id,provider_idempotency_key,provider_request,provider_request_hash,provider_reference,provider_state,provider_first_attempt_at,provider_retry_until FROM "{schema}".payments WHERE id=:id'), {"id": payment.id}).one()
            assert tuple(row) == expected
        with pytest.raises(IntegrityError):
            with target.begin() as c:
                c.execute(text(f'UPDATE "{schema}".payments SET provider_request_hash=:hash WHERE id=:id'), {"hash": "0"*64, "id": payment.id})
    finally:
        if attempted:
            with target.begin() as c:c.execute(DropSchema(schema, cascade=True))
        target.dispose()


@pytest.mark.parametrize("options", [
    {"processing_backend": "live"},
    {"processing_backend": "stripe", "provider_account_id": "invalid-account", "payment_method": "pm_card_visa"},
    {"processing_backend": "stripe", "provider_account_id": ACCOUNT, "payment_method": "raw-card-data"},
])
def test_invalid_server_context_does_not_persist_partial_rows(migrated, options):
    with migrated() as session:
        merchant_id = owner(session)
        with pytest.raises(ValueError):
            service(session).create_payment(payload(), merchant_id=merchant_id, **options)
        session.commit()
        assert session.scalar(select(func.count(Payment.id))) == 0
        assert session.scalar(select(func.count(PaymentEvent.id))) == 0


def test_provider_key_does_not_require_a_client_idempotency_key(migrated):
    with migrated() as session:
        merchant_id = owner(session)
        p = service(session).create_payment(payload(), merchant_id=merchant_id,
            processing_backend="stripe", provider_account_id=ACCOUNT, payment_method="pm_card_visa")
        assert p.provider_idempotency_key and session.scalar(select(func.count(IdempotencyKey.id))) == 0
        assert session.scalar(select(func.count(PaymentEvent.id))) == 1


def test_unknown_local_revision_fails_without_rewriting_history(pg_engine):
    from alembic.util import CommandError
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "a001_dashboard_sessions")
    with pg_engine.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num='0bb39d7d985d'"))
    with pytest.raises(CommandError):migrate(pg_engine, "head")
    with pg_engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0bb39d7d985d"
        assert c.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='payments' AND column_name='processing_backend'")) == 0


def test_legacy_only_downgrade_and_reupgrade_preserve_data(pg_engine):
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "a001_dashboard_sessions")
    payment_id = uuid4()
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO payments(id,merchant_reference,amount,currency,customer_email,status,retry_count) VALUES(:id,'legacy-roundtrip',12,'EUR','history@example.com','COMPLETED',0)"), {"id": payment_id})
    migrate(pg_engine, "head")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with pg_engine.begin() as c:
        config.attributes["connection"] = c
        command.downgrade(config, "a001_dashboard_sessions")
        assert c.scalar(text("SELECT merchant_reference FROM payments WHERE id=:id"), {"id": payment_id}) == "legacy-roundtrip"
    migrate(pg_engine, "head")
    with pg_engine.connect() as c:
        assert c.scalar(text("SELECT status::text FROM payments WHERE id=:id"), {"id": payment_id}) == "COMPLETED"
        assert c.scalar(text("SELECT processing_backend FROM payments WHERE id=:id"), {"id": payment_id}) is None


def test_truncate_cannot_erase_operation_history(migrated):
    with migrated() as session:
        payment = stripe_payment(session, owner(session))
        with pytest.raises(IntegrityError):
            session.execute(text("TRUNCATE payments CASCADE"))
            session.commit()
        session.rollback()
        assert session.get(Payment, payment.id).provider_state == "prepared"
        assert session.scalar(select(func.count(PaymentEvent.id))) == 1


def test_malformed_provider_reference_cannot_be_bound(migrated):
    with migrated() as session:
        payment = stripe_payment(session, owner(session))
        with pytest.raises(IntegrityError):
            start_reconciliation(session, payment, "not-a-provider-reference")
        session.rollback()
        assert session.get(Payment, payment.id).provider_reference is None


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "review_required", "requires_action"])
def test_legitimate_provider_transitions_preserve_original_contract(migrated, terminal):
    with migrated() as session:
        payment = stripe_payment(session, owner(session))
        contract = (payment.provider_idempotency_key, payment.provider_request, payment.provider_request_hash,
                    payment.merchant_id, payment.provider_account_id, payment.amount, payment.currency)
        start_reconciliation(session, payment, "pi_transitionSynthetic")
        payment.provider_state = "pending"
        payment.provider_status = "processing"
        session.commit()
        payment.provider_state = terminal
        payment.provider_status = {"succeeded": "succeeded", "failed": "canceled",
                                   "review_required": "processing", "requires_action": "requires_action"}[terminal]
        payment.status = {"succeeded": PaymentStatus.COMPLETED, "failed": PaymentStatus.FAILED}.get(terminal, PaymentStatus.PROCESSING)
        session.commit()
        session.refresh(payment)
        assert payment.provider_state == terminal
        assert (payment.provider_idempotency_key, payment.provider_request, payment.provider_request_hash,
                payment.merchant_id, payment.provider_account_id, payment.amount, payment.currency) == contract
