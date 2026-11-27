"""PostgreSQL-only authentication, migration, ownership, and recovery checks."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import os
import shutil
import subprocess
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from test_queue_concurrency import pg_engine, session_factory
from app.api.deps import get_db_session
from app.core.config import settings
from app.db.session import Base
from app.main import app
from app.models.auth import DashboardUser
from app.models.payment import IdempotencyKey, Merchant, Payment, PaymentEvent
from app.queue.postgres import PostgresQueueConsumer
from app.repositories import SqlAlchemyMerchantRepository, SqlAlchemyPaymentRepository
from app.repositories.auth_repository import SqlAlchemyAuthRepository
from app.services.auth_service import AuthService
from app.services.merchant_service import MerchantService
from app.services.payment_service import PaymentService
from app.queue import PostgresQueuePublisher
from app.api.deps import build_worker_service


@pytest.fixture(autouse=True)
def clean_migration_revision(pg_engine):
    yield
    with pg_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


PASSWORD = "synthetic-postgres-password"
ORIGIN = "http://localhost:5173"
HEAD_REVISION = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations")).get_current_head()


def migrate(engine, target):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, target)


def payment_payload(reference="pg-order"):
    from app.schemas.payment import PaymentCreate
    return PaymentCreate(merchant_reference=reference, amount="12.00", currency="EUR", customer_email="fixture@example.com")


def test_historical_migration_preserves_rows_and_quarantines_queue(pg_engine, session_factory):
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "72888bf6baba")
    payment_id, event_id, key_id = uuid4(), uuid4(), uuid4()
    with pg_engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO payments(id,merchant_reference,amount,currency,customer_email,status,retry_count) "
            "VALUES(:id,'historical-order',12,'EUR','fixture@example.com','PENDING',0)"
        ), {"id": payment_id})
        connection.execute(text(
            "INSERT INTO payment_events(id,payment_id,queue_name,event_type,payload,status,attempts) "
            "VALUES(:id,:payment,:queue,'payment.created','{}','PENDING',0)"
        ), {"id": event_id, "payment": payment_id, "queue": settings.payment_queue_name})
        connection.execute(text(
            "INSERT INTO idempotency_keys(id,key,request_hash,payment_id) VALUES(:id,'historical-key','synthetic-hash',:payment)"
        ), {"id": key_id, "payment": payment_id})
    migrate(pg_engine, "head")
    with session_factory() as session:
        old = session.get(Payment, payment_id)
        assert old.amount == Decimal("12.00") and old.merchant_id is None
        assert session.get(IdempotencyKey, key_id).merchant_id is None
        assert session.get(PaymentEvent, event_id).attempts == 0
        assert PostgresQueueConsumer(session).claim_available(batch_size=10) == []
        assert session.scalar(select(func.count(Merchant.id))) == 0


def test_interrupted_migration_rolls_back_and_can_be_retried(pg_engine, monkeypatch):
    from alembic import op
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "72888bf6baba")
    payment_id = uuid4()
    with pg_engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO payments(id,merchant_reference,amount,currency,customer_email,status,retry_count) "
            "VALUES(:id,'interrupted-history',12,'EUR','fixture@example.com','PENDING',0)"
        ), {"id": payment_id})
    create_table = op.create_table
    def interrupted(name, *args, **kwargs):
        if name == "dashboard_sessions":
            raise RuntimeError("Synthetic interruption")
        return create_table(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(op, "create_table", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic interruption"):
            migrate(pg_engine, "head")
    with pg_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "72888bf6baba"
        assert connection.scalar(text("SELECT to_regclass('dashboard_users')")) is None
        assert connection.scalar(text("SELECT to_regclass('merchants')")) is None
        assert connection.scalar(text("SELECT merchant_reference FROM payments WHERE id=:id"), {"id": payment_id}) == "interrupted-history"
    migrate(pg_engine, "head")
    with pg_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert connection.scalar(text("SELECT merchant_id FROM payments WHERE id=:id"), {"id": payment_id}) is None


def test_login_throttle_counts_concurrent_attempts_atomically(session_factory):
    def attempt(_):
        with session_factory() as session:
            return SqlAlchemyAuthRepository(session).consume_attempt("synthetic-concurrent-source", 3)
    with ThreadPoolExecutor(max_workers=8) as executor:
        allowed = list(executor.map(attempt, range(8)))
    assert sum(allowed) == 3


def test_already_expanded_schema_remains_compatible(pg_engine):
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "00cf512790ec")
    merchant_id, payment_id, key_id = uuid4(), uuid4(), uuid4()
    with pg_engine.begin() as connection:
        connection.execute(text("ALTER TABLE idempotency_keys DROP CONSTRAINT fk_idempotency_payment_owner"))
        connection.execute(text("ALTER TABLE payments DROP CONSTRAINT uq_payment_owner"))
        connection.execute(text("DROP INDEX uq_legacy_payment_reference"))
        connection.execute(text("DROP INDEX uq_legacy_idempotency_key"))
        for table in ("payments", "idempotency_keys"):
            connection.execute(text(f"ALTER TABLE {table} ALTER COLUMN merchant_id SET NOT NULL"))
            connection.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_merchant"))
            connection.execute(text(f"ALTER TABLE {table} ADD FOREIGN KEY(merchant_id) REFERENCES merchants(id) ON DELETE CASCADE"))
        connection.execute(text("INSERT INTO merchants(id,name) VALUES(:id,'Existing owner')"), {"id": merchant_id})
        connection.execute(text(
            "INSERT INTO payments(id,merchant_id,merchant_reference,amount,currency,customer_email,status,retry_count) "
            "VALUES(:id,:owner,'already-owned',12,'EUR','fixture@example.com','PENDING',0)"
        ), {"id": payment_id, "owner": merchant_id})
        connection.execute(text(
            "INSERT INTO idempotency_keys(id,merchant_id,key,request_hash,payment_id) "
            "VALUES(:id,:owner,'owned-key','synthetic-hash',:payment)"
        ), {"id": key_id, "owner": merchant_id, "payment": payment_id})
    migrate(pg_engine, "head")
    with pg_engine.connect() as connection:
        columns = connection.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='payments' AND column_name='merchant_id'"
        )).scalars().all()
        assert columns == ["YES"]
        assert connection.scalar(text("SELECT merchant_id FROM payments WHERE id=:id"), {"id": payment_id}) == merchant_id
        assert connection.scalar(text("SELECT merchant_id FROM idempotency_keys WHERE id=:id"), {"id": key_id}) == merchant_id


def test_cookie_flow_worker_and_tenant_isolation_on_postgres(pg_engine, session_factory):
    with session_factory() as session:
        merchants = MerchantService(repository=SqlAlchemyMerchantRepository(session))
        first, _ = merchants.create_merchant("First", issue_api_key=False)
        second, _ = merchants.create_merchant("Second", issue_api_key=False)
        auth = AuthService(SqlAlchemyAuthRepository(session), settings)
        auth.create_user(first.id, "first@example.com", PASSWORD)
        auth.create_user(second.id, "second@example.com", PASSWORD)

    def override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    try:
        with TestClient(app) as first_client, TestClient(app) as second_client:
            for client, email in ((first_client, "first@example.com"), (second_client, "second@example.com")):
                csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
                login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD},
                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
                assert login.status_code == 200
                client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]})
            data = payment_payload().model_dump(mode="json")
            one = first_client.post("/api/v1/payments", json=data, headers={"Idempotency-Key": "same-key"})
            replay = first_client.post("/api/v1/payments", json=data, headers={"Idempotency-Key": "same-key"})
            two = second_client.post("/api/v1/payments", json=data, headers={"Idempotency-Key": "same-key"})
            assert one.status_code == replay.status_code == two.status_code == 201
            assert one.json()["id"] == replay.json()["id"] != two.json()["id"]
            for suffix in ("", "/events", "/processing"):
                assert second_client.get("/api/v1/payments/" + one.json()["id"] + suffix).status_code == 404
            with session_factory() as session:
                result = build_worker_service(session).process_available_events()
                assert result.completed == 2
            assert first_client.get("/api/v1/payments/" + one.json()["id"]).json()["status"] == "COMPLETED"
            assert first_client.post("/api/v1/auth/logout").status_code == 204
            assert first_client.get("/api/v1/payments").status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_concurrent_idempotent_requests_publish_once(pg_engine, session_factory):
    with session_factory() as session:
        merchant, _ = MerchantService(repository=SqlAlchemyMerchantRepository(session)).create_merchant("Concurrent")
        merchant_id = merchant.id

    def create():
        with session_factory() as session:
            return PaymentService(
                repository=SqlAlchemyPaymentRepository(session), publisher=PostgresQueuePublisher(session)
            ).create_payment(payment_payload("concurrent-order"), merchant_id=merchant_id, idempotency_key="one-key").id

    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(lambda _: create(), range(4)))
    assert len(set(identifiers)) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count(Payment.id))) == 1
        assert session.scalar(select(func.count(PaymentEvent.id))) == 1


def test_database_rejects_cross_tenant_idempotency(pg_engine, session_factory):
    from sqlalchemy.exc import IntegrityError
    with session_factory() as session:
        service = MerchantService(repository=SqlAlchemyMerchantRepository(session))
        one, _ = service.create_merchant("One")
        two, _ = service.create_merchant("Two")
        payment = PaymentService(repository=SqlAlchemyPaymentRepository(session), publisher=PostgresQueuePublisher(session))
        created = payment.create_payment(payment_payload(), merchant_id=one.id)
        session.add(IdempotencyKey(merchant_id=two.id, key="wrong-owner", request_hash="synthetic", payment_id=created.id))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_recovery_backup_restores_synthetic_data(pg_engine, session_factory, tmp_path):
    raw = os.getenv("TEST_BACKUP_POSTGRES_URL")
    if not raw:
        pytest.skip("Set TEST_BACKUP_POSTGRES_URL to validate a disposable restore")
    target_url = make_url(raw)
    if target_url.drivername not in {"postgresql", "postgresql+psycopg"} or not (target_url.database or "").endswith("_test"):
        pytest.fail("Restore database must be an explicitly configured PostgreSQL test database", pytrace=False)
    if target_url.database == pg_engine.url.database:
        pytest.fail("Restore requires an independent test database", pytrace=False)
    bindir = os.getenv("TEST_POSTGRES_BIN", "")
    dump = str(Path(bindir) / "pg_dump") if bindir else shutil.which("pg_dump")
    restore = str(Path(bindir) / "pg_restore") if bindir else shutil.which("pg_restore")
    if not dump or not restore:
        pytest.skip("PostgreSQL backup tools are unavailable")
    with session_factory() as session:
        merchant, _ = MerchantService(repository=SqlAlchemyMerchantRepository(session)).create_merchant("Backup", issue_api_key=False)
        payment = PaymentService(repository=SqlAlchemyPaymentRepository(session), publisher=PostgresQueuePublisher(session)).create_payment(
            payment_payload("backup-order"), merchant_id=merchant.id)
        expected = (str(payment.id), payment.merchant_reference, payment.amount)
        schema = session.scalar(select(func.current_schema()))
    import re
    assert re.fullmatch(r"pulsepay_test_[a-f0-9]{32}", schema)

    def pg_env(url):
        env = os.environ.copy()
        env.update(PGHOST=str(url.query.get("host", url.host or "")), PGPORT=str(url.query.get("port", url.port or 5432)),
                   PGUSER=url.username or "", PGDATABASE=url.database,
                   PGPASSFILE=str(tmp_path / "unused-password-file"))
        if url.password:
            env["PGPASSWORD"] = url.password
        else:
            env.pop("PGPASSWORD", None)
        return env

    backup = tmp_path / "synthetic-backup.dump"
    result = subprocess.run([dump, "--no-owner", "--no-privileges", "--format=custom", "--schema", schema, "--file", str(backup)],
                            env=pg_env(pg_engine.url), capture_output=True, timeout=20)
    assert result.returncode == 0
    target = create_engine(target_url.set(drivername="postgresql+psycopg"), hide_parameters=True, connect_args={"connect_timeout": 3})
    attempted_restore = False
    try:
        with target.connect() as connection:
            assert connection.scalar(select(func.current_database())) == target_url.database
            assert not connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"), {"schema": schema})
        attempted_restore = True
        result = subprocess.run([restore, "--no-owner", "--no-privileges", "--exit-on-error", "--dbname", target_url.database, str(backup)],
                                env=pg_env(target_url), capture_output=True, timeout=20)
        assert result.returncode == 0
        with target.connect() as connection:
            restored = connection.execute(text(f'SELECT id,merchant_reference,amount FROM "{schema}".payments')).one()
            assert (str(restored.id), restored.merchant_reference, restored.amount) == expected
    finally:
        if attempted_restore:
            from sqlalchemy.schema import DropSchema
            with target.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        target.dispose()


def test_downgrade_refuses_to_remove_user_information(pg_engine, session_factory):
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "head")
    with session_factory() as session:
        merchant, _ = MerchantService(repository=SqlAlchemyMerchantRepository(session)).create_merchant("Recovery", issue_api_key=False)
        AuthService(SqlAlchemyAuthRepository(session), settings).create_user(merchant.id, "recovery@example.com", PASSWORD)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with pytest.raises(RuntimeError, match="recovery plan"):
        with pg_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "72888bf6baba")
    with session_factory() as session:
        assert session.scalar(select(func.count(DashboardUser.id))) == 1
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION


def test_migrations_match_models(pg_engine):
    Base.metadata.drop_all(pg_engine)
    migrate(pg_engine, "head")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with pg_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.check(config)


def test_password_reset_serializes_with_login_and_revokes_new_session(pg_engine, session_factory, monkeypatch):
    from concurrent.futures import TimeoutError
    from threading import Event
    import app.services.auth_service as auth_module
    from app.services.auth_service import AuthError

    with session_factory() as session:
        merchant, _ = MerchantService(repository=SqlAlchemyMerchantRepository(session)).create_merchant("Password race", issue_api_key=False)
        service = AuthService(SqlAlchemyAuthRepository(session), settings)
        service.create_user(merchant.id, "race@example.com", PASSWORD)
        token, record = service.csrf(None, "synthetic-source")
        csrf = record.csrf_token
    verifying = Event()
    release = Event()
    original_verify = auth_module.verify_password

    def wait_verify(password, encoded):
        verifying.set()
        assert release.wait(5)
        return original_verify(password, encoded)

    monkeypatch.setattr(auth_module, "verify_password", wait_verify)

    def login():
        with session_factory() as session:
            return AuthService(SqlAlchemyAuthRepository(session), settings).login(
                token, csrf, "race@example.com", PASSWORD, "synthetic-source")[0]

    def reset():
        with session_factory() as session:
            repository = SqlAlchemyAuthRepository(session)
            AuthService(repository, settings).set_password(
                repository.get_user("race@example.com"), "synthetic-replacement-password")

    with ThreadPoolExecutor(max_workers=2) as pool:
        login_future = pool.submit(login)
        assert verifying.wait(5)
        reset_future = pool.submit(reset)
        try:
            with pytest.raises(TimeoutError):
                reset_future.result(timeout=0.2)
        finally:
            release.set()
        authenticated_token = login_future.result(timeout=5)
        reset_future.result(timeout=5)
    with session_factory() as session:
        with pytest.raises(AuthError):
            AuthService(SqlAlchemyAuthRepository(session), settings).authenticate(authenticated_token)
