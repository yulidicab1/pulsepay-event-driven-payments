"""Opt-in browser fixture: disposable PostgreSQL schema, synthetic users and fake Stripe."""
from pathlib import Path
import os
import signal
import sys
import tempfile
from threading import Event, Thread
from urllib.parse import urlsplit
from uuid import uuid4


def main():
    if os.getenv("PULSEPAY_E2E_ALLOWED") != "1" or not os.getenv("E2E_POSTGRES_URL"):
        raise SystemExit("Browser tests require explicit disposable PostgreSQL configuration.")
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError
    try:
        url = make_url(os.environ["E2E_POSTGRES_URL"])
        if url.drivername not in {"postgresql", "postgresql+psycopg"} or not (url.database or "").endswith("_test") or url.host not in {"127.0.0.1", "localhost", "postgres"}:
            raise ValueError
        origin = os.environ["E2E_FRONTEND_ORIGIN"]
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path:
            raise ValueError
        port = int(os.environ.get("E2E_API_PORT", "18081"))
        if not 1024 < port < 65536: raise ValueError
    except (ArgumentError, KeyError, ValueError):
        raise SystemExit("Invalid isolated browser test configuration.") from None
    root = Path(__file__).resolve().parents[1]
    schema = "pulsepay_test_" + uuid4().hex
    stop = Event()
    with tempfile.TemporaryDirectory(prefix="pulsepay-browser-") as scratch:
        # Settings cannot load a developer .env or inherited provider credential.
        os.chdir(scratch)
        path = os.environ.get("PATH", "")
        database = url.set(drivername="postgresql+psycopg", query={**url.query, "options": "-csearch_path=" + schema})
        os.environ.clear()
        os.environ.update(PATH=path, APP_ENV="test", PUBLIC_ORIGIN=origin,
                          DATABASE_URL=database.render_as_string(hide_password=False),
                          PAYMENT_RETRY_DELAY_SECONDS="1", PYTHONDONTWRITEBYTECODE="1")
        sys.path.insert(0, str(root))
        from dataclasses import replace
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import text
        from sqlalchemy.schema import CreateSchema, DropSchema
        import uvicorn
        from app.db.session import SessionLocal, engine
        from app.db import base
        from app.main import app
        from app.repositories import SqlAlchemyMerchantRepository
        from app.repositories.auth_repository import SqlAlchemyAuthRepository
        from app.services.auth_service import AuthService
        from app.services.merchant_service import MerchantService
        from app.core.config import settings
        from app.services.stripe_gateway import GatewayStatus
        from test_stripe_recovery import Provider, worker
        from test_postgres_stripe import stripe_payment, ACCOUNT

        class DashboardProvider(Provider):
            policies = {}

            def adjust(self, result):
                policy = self.policies[dict(result.metadata)["payment_id"]]
                if policy == "unknown": raise TimeoutError("Synthetic lost response")
                if policy == "review": return replace(result, account_id="acct_syntheticMismatch")
                if policy == "failed": return replace(result, status=GatewayStatus.DECLINED, provider_status="canceled", amount_received=0)
                if policy in {"processing", "action"}:
                    return replace(result, status=GatewayStatus.PENDING, amount_received=0,
                                   provider_status="requires_action" if policy == "action" else "processing")
                return result

            def create_payment(self, **params):
                return self.adjust(super().create_payment(**params))

            def retrieve_payment(self, reference):
                return self.adjust(super().retrieve_payment(reference))

        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        def upgrade(target):
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, target)

        with engine.begin() as connection: connection.execute(CreateSchema(schema))
        threads = []
        try:
            upgrade("a001_dashboard_sessions")
            with SessionLocal() as session:
                merchants = [MerchantService(repository=SqlAlchemyMerchantRepository(session)).create_merchant(name, issue_api_key=False)[0]
                             for name in ("Synthetic Alpha", "Synthetic Beta")]
                merchant_ids = [merchant.id for merchant in merchants]
                session.execute(text("INSERT INTO payments(id,merchant_id,merchant_reference,amount,currency,customer_email,status,retry_count) VALUES(:id,:merchant,'historical-unclassified',12,'EUR','synthetic@example.com','PENDING',0)"),
                                {"id": uuid4(), "merchant": merchant_ids[0]})
                session.commit()
            upgrade("head")
            provider = DashboardProvider()
            with SessionLocal() as session:
                auth = AuthService(SqlAlchemyAuthRepository(session), settings)
                for merchant_id, email in zip(merchant_ids, ("e2e-alpha@example.com", "e2e-beta@example.com")):
                    auth.create_user(merchant_id, email, "Synthetic-browser-password-2026!")
                for policy in ("completed", "failed", "processing", "unknown", "review", "action"):
                    payment = stripe_payment(session, merchant_ids[0], reference="stripe-" + policy, key="synthetic-http-" + policy)
                    provider.policies[str(payment.id)] = policy
                payment = stripe_payment(session, merchant_ids[1], reference="beta-private-payment")
                provider.policies[str(payment.id)] = "completed"
                worker(session, provider).process_available_events(batch_size=20)

            def process():
                while not stop.is_set():
                    with SessionLocal() as session: worker(session, provider).process_available_events()
                    stop.wait(0.2)
            for _ in range(2):
                thread = Thread(target=process, daemon=True);thread.start();threads.append(thread)
            # Let Uvicorn finish gracefully and then clean only this generated schema.
            for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, lambda *_: None)
            uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
                                         log_level="warning", timeout_graceful_shutdown=5)).run()
        finally:
            stop.set()
            for thread in threads: thread.join(5)
            with engine.begin() as connection: connection.execute(DropSchema(schema, cascade=True))
            engine.dispose()


if __name__ == "__main__":
    main()
