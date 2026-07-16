"""Concurrency tests for the PostgreSQL-backed queue consumer.

These tests demonstrate the "double claim" race condition and prove the
`FOR UPDATE SKIP LOCKED` fix in `PostgresQueueConsumer.claim_available`.

They require a real PostgreSQL instance because SQLite (used by the rest of
the suite) serializes all access and cannot reproduce row-level locking. The
module is skipped automatically when Postgres is not reachable, so the default
`pytest` run stays green without extra infrastructure.

WARNING: the fixtures create and drop all tables on TEST_POSTGRES_URL, so point
it at a disposable database, not one holding data you care about.

Run against the local Docker Compose database with:

    docker compose up -d postgres
    TEST_POSTGRES_URL=postgresql+psycopg://pulsepay:pulsepay@localhost:5432/pulsepay \
        backend/.venv/bin/pytest backend/tests/test_queue_concurrency.py -v
"""

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models.payment import Payment, PaymentEvent, PaymentStatus, QueueMessageStatus
from app.queue.postgres import PostgresQueueConsumer

POSTGRES_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql+psycopg://pulsepay:pulsepay@localhost:5432/pulsepay",
)


def _postgres_available() -> bool:
    try:
        probe_engine = create_engine(POSTGRES_URL)
        with probe_engine.connect():
            return True
    except Exception:
        return False
    finally:
        try:
            probe_engine.dispose()
        except Exception:
            pass


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason=f"PostgreSQL not reachable at {POSTGRES_URL}; skipping concurrency tests",
)


@pytest.fixture
def pg_engine():
    engine = create_engine(POSTGRES_URL)
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
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
