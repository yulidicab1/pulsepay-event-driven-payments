import os
import sys
from collections.abc import Generator
from pathlib import Path
from tempfile import gettempdir

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_DB_PATH = Path(gettempdir()) / "pulsepay_test.db"
TEST_DATABASE_URL = f"sqlite:///{TEST_DB_PATH}"

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["APP_ENV"] = "test"
os.environ["PUBLIC_ORIGIN"] = "http://localhost:5173"

from app.api.deps import get_db_session
from app.db.session import Base
from app.main import app

engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def reset_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


TEST_API_KEY = "sk_test_conftestkey"


@pytest.fixture
def merchant(db_session: Session):
    """Create a merchant with a known API key for authenticated requests."""
    from app.repositories import SqlAlchemyMerchantRepository
    from app.services.merchant_service import MerchantService

    service = MerchantService(repository=SqlAlchemyMerchantRepository(db_session))
    record, _ = service.create_merchant("Test Merchant", api_key=TEST_API_KEY)
    return record


@pytest.fixture
def client(db_session: Session, merchant) -> Generator[TestClient, None, None]:
    def override_get_db_session() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    with TestClient(app) as test_client:
        # Every request is authenticated as the test merchant by default.
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def anon_client(db_session: Session) -> Generator[TestClient, None, None]:
    """A client with no default Authorization header, for auth tests."""
    def override_get_db_session() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
