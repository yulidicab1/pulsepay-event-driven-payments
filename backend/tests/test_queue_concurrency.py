"""Concurrency tests for the PostgreSQL-backed queue consumer.

These tests demonstrate the "double claim" race condition and prove the
`FOR UPDATE SKIP LOCKED` fix in `PostgresQueueConsumer.claim_available`.

They require a dedicated PostgreSQL database configured with TEST_POSTGRES_URL.
Each test uses its own schema. See the README for setup.
"""

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.config import settings
from app.db.session import Base
from app.models.payment import Payment, PaymentEvent, PaymentStatus, QueueMessageStatus
from app.queue.postgres import PostgresQueueConsumer

@pytest.fixture
def pg_engine():
    raw_url = os.getenv("TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("Set TEST_POSTGRES_URL to run the PostgreSQL concurrency tests")

    try:
        url = make_url(raw_url)
    except (ArgumentError, ValueError):
        raise pytest.fail.Exception("TEST_POSTGRES_URL is not a valid database URL", pytrace=False) from None
    if (
        url.drivername not in {"postgresql", "postgresql+psycopg"}
        or not (url.database or "").endswith("_test")
    ):
        pytest.fail("TEST_POSTGRES_URL must point to a PostgreSQL database ending in _test", pytrace=False)

    schema = f"pulsepay_test_{uuid.uuid4().hex}"
    engine = create_engine(
        url.set(drivername="postgresql+psycopg"),
        connect_args={"connect_timeout": 3, "options": f"-csearch_path={schema}"},
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            if connection.scalar(select(func.current_database())) != url.database:
                pytest.fail("Connected to a different database than TEST_POSTGRES_URL names", pytrace=False)
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection)

        yield engine

        with engine.begin() as connection:
            Base.metadata.drop_all(connection)
            connection.execute(DropSchema(schema))
    except (SQLAlchemyError, ValueError):
        raise pytest.fail.Exception(
            "Could not set up or clean up the isolated PostgreSQL test schema", pytrace=False
        ) from None
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    return sessionmaker(bind=pg_engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _seed_pending_event(session_factory) -> uuid.UUID:
    """Insert one payment plus one PENDING queue message and commit it."""
    with session_factory() as setup_session:
        payment = Payment(
            merchant_reference=f"concurrency-{uuid.uuid4().hex[:8]}",
            amount=Decimal("120.00"),
            currency="USD",
            customer_email="ops@example.com",
            status=PaymentStatus.PENDING,
        )
        setup_session.add(payment)
        setup_session.flush()
        event = PaymentEvent(
            payment_id=payment.id,
            queue_name=settings.payment_queue_name,
            event_type="payment.created",
            payload={"payment_id": str(payment.id)},
            status=QueueMessageStatus.PENDING,
        )
        setup_session.add(event)
        setup_session.commit()
        return event.id


def test_second_worker_skips_message_locked_by_first_worker(session_factory):
    """With the fix, a message claimed by one worker is invisible to another.

    Worker A claims the message inside an open transaction (holding the row
    lock). Worker B must receive zero messages instead of claiming the same
    one, which is exactly what FOR UPDATE SKIP LOCKED guarantees.
    """
    _seed_pending_event(session_factory)

    session_a = session_factory()
    session_b = session_factory()
    try:
        claimed_a = PostgresQueueConsumer(session_a).claim_available(batch_size=10)
        assert len(claimed_a) == 1  # worker A holds the row lock, transaction open

        claimed_b = PostgresQueueConsumer(session_b).claim_available(batch_size=10)
        assert claimed_b == []  # worker B skips the locked row instead of double-claiming

        session_a.commit()
    finally:
        session_a.close()
        session_b.close()


def test_unlocked_select_reproduces_the_double_claim_bug(session_factory):
    """Baseline showing the original (unlocked) query lets both workers claim.

    This mirrors the pre-fix `claim_available` implementation: a plain SELECT
    with no row lock. Under READ COMMITTED, worker B still sees the row as
    PENDING while worker A's UPDATE is uncommitted, so both claim it. This is
    the concrete bug that the SKIP LOCKED fix removes.
    """
    _seed_pending_event(session_factory)

    unlocked_statement = (
        select(PaymentEvent)
        .where(PaymentEvent.status == QueueMessageStatus.PENDING)
        .where(PaymentEvent.queue_name == settings.payment_queue_name)
        .where(PaymentEvent.available_at <= datetime.now(UTC))
        .order_by(PaymentEvent.created_at.asc())
        .limit(10)
    )

    session_a = session_factory()
    session_b = session_factory()
    try:
        events_a = list(session_a.scalars(unlocked_statement))
        assert len(events_a) == 1
        for event in events_a:
            event.status = QueueMessageStatus.PROCESSING
            event.attempts += 1
        session_a.flush()  # UPDATE sent but NOT committed

        events_b = list(session_b.scalars(unlocked_statement))
        assert len(events_b) == 1  # BUG: worker B claims the very same message

        assert events_a[0].id == events_b[0].id

        session_a.rollback()
    finally:
        session_a.close()
        session_b.close()
