from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings, settings
from app.core.security import hash_api_key, hash_password
from app.models.auth import DashboardSession, DashboardUser
from app.models.payment import MerchantApiKey, Payment, PaymentStatus
from app.repositories import SqlAlchemyMerchantRepository, SqlAlchemyPaymentRepository
from app.repositories.auth_repository import SqlAlchemyAuthRepository
from app.services.auth_service import AuthService
from app.services.merchant_service import MerchantService

PASSWORD = "synthetic-test-password-2026"
ORIGIN = "http://localhost:5173"


@pytest.fixture(scope="session")
def encoded_password():
    return hash_password(PASSWORD)


@pytest.fixture
def dashboard_user(db_session, merchant, encoded_password):
    user = DashboardUser(merchant_id=merchant.id, email="operator@example.com", password_hash=encoded_password)
    db_session.add(user)
    db_session.commit()
    return user


def sign_in(client, email="operator@example.com", password=PASSWORD, origin=ORIGIN):
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password},
                           headers={"Origin": origin, "X-CSRF-Token": csrf})
    if response.status_code == 200:
        client.headers.update({"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]})
    return response


def payload(reference="session-order"):
    return {"merchant_reference": reference, "amount": "12.00", "currency": "EUR", "customer_email": "fixture@example.com"}


def test_login_rotates_cookie_and_logout_revokes(anon_client, dashboard_user, db_session):
    csrf = anon_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    previous = anon_client.cookies.get(settings.session_cookie_name)
    response = sign_in(anon_client)
    assert response.status_code == 200
    token = anon_client.cookies.get(settings.session_cookie_name)
    assert token != previous
    assert response.json()["csrf_token"] != csrf
    assert db_session.get(DashboardSession, hash_api_key(previous)) is None
    assert db_session.get(DashboardSession, hash_api_key(token)).user_id == dashboard_user.id
    assert token not in response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert "domain=" not in response.headers["set-cookie"].lower()
    assert anon_client.get("/api/v1/auth/session").status_code == 200
    assert anon_client.get("/api/v1/payments").status_code == 200
    assert anon_client.post("/api/v1/auth/logout").status_code == 204
    anon_client.cookies.set(settings.session_cookie_name, token)
    assert anon_client.get("/api/v1/payments").status_code == 401


@pytest.mark.parametrize("failure", ["missing-csrf", "wrong-csrf", "origin"])
def test_login_csrf_checks(anon_client, dashboard_user, failure):
    csrf = anon_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    if failure == "missing-csrf":
        del headers["X-CSRF-Token"]
    elif failure == "wrong-csrf":
        headers["X-CSRF-Token"] = "synthetic-wrong-token"
    else:
        headers["Origin"] = "https://attacker.example.com"
    response = anon_client.post("/api/v1/auth/login",
        json={"email": dashboard_user.email, "password": PASSWORD}, headers=headers)
    assert response.status_code == 403
    assert anon_client.get("/api/v1/payments").status_code == 401


def test_session_csrf_protects_payment_and_logout(anon_client, dashboard_user):
    assert sign_in(anon_client).status_code == 200
    csrf = anon_client.headers.pop("X-CSRF-Token")
    assert anon_client.post("/api/v1/payments", json=payload()).status_code == 403
    assert anon_client.post("/api/v1/auth/logout").status_code == 403
    anon_client.headers["X-CSRF-Token"] = csrf
    assert anon_client.post("/api/v1/payments", json=payload()).status_code == 201


@pytest.mark.parametrize("kind", ["absolute", "idle", "disabled"])
def test_expiration_and_disabled_accounts(anon_client, dashboard_user, db_session, kind):
    assert sign_in(anon_client).status_code == 200
    record = db_session.get(DashboardSession, hash_api_key(anon_client.cookies.get(settings.session_cookie_name)))
    if kind == "absolute":
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif kind == "idle":
        record.last_seen_at = datetime.now(UTC) - timedelta(seconds=settings.session_idle_seconds + 1)
    else:
        dashboard_user.active = False
    db_session.commit()
    assert anon_client.get("/api/v1/payments").status_code == 401
    assert anon_client.post("/api/v1/payments", json=payload()).status_code == 401


def test_password_reset_revokes_sessions(anon_client, dashboard_user, db_session):
    assert sign_in(anon_client).status_code == 200
    service = AuthService(SqlAlchemyAuthRepository(db_session), settings)
    service.set_password(dashboard_user, "synthetic-replacement-password")
    assert anon_client.get("/api/v1/auth/session").status_code == 401
    assert sign_in(anon_client).status_code == 401
    assert sign_in(anon_client, password="synthetic-replacement-password").status_code == 200


def test_invalid_credentials_are_generic_and_throttled(anon_client, dashboard_user):
    wrong = sign_in(anon_client, password="synthetic-wrong-password")
    unknown = sign_in(anon_client, email="missing@example.com", password="synthetic-wrong-password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Invalid credentials."}
    for _ in range(settings.login_account_limit - 1):
        assert sign_in(anon_client, password="synthetic-wrong-password").status_code == 401
    assert sign_in(anon_client, password="synthetic-wrong-password").status_code == 429


def test_account_throttle_is_shared_by_repository_instances(db_session):
    first = SqlAlchemyAuthRepository(db_session)
    second = SqlAlchemyAuthRepository(db_session)
    assert first.consume_attempt("synthetic-source", 1)
    assert not second.consume_attempt("synthetic-source", 1)


def test_invalid_input_never_echoes_password(anon_client):
    marker = "synthetic-sensitive-value"
    result = anon_client.post("/api/v1/auth/login", json={"email": "invalid", "password": marker}, headers={"Origin": ORIGIN})
    assert result.status_code == 422
    assert marker not in result.text


def test_session_cannot_read_another_merchants_resources(anon_client, dashboard_user, db_session):
    other, _ = MerchantService(repository=SqlAlchemyMerchantRepository(db_session)).create_merchant("Other", issue_api_key=False)
    foreign = Payment(merchant_id=other.id, merchant_reference="foreign-order", amount=10,
                      currency="EUR", customer_email="fixture@example.com", status=PaymentStatus.PENDING)
    db_session.add(foreign)
    db_session.commit()
    assert sign_in(anon_client).status_code == 200
    for suffix in ("", "/events", "/processing"):
        assert anon_client.get(f"/api/v1/payments/{foreign.id}{suffix}").status_code == 404
    assert anon_client.get("/api/v1/payments").json() == []
    assert anon_client.get("/api/v1/payments/metrics").json()["total"] == 0
    repository = SqlAlchemyPaymentRepository(db_session)
    assert repository.get(foreign.id, dashboard_user.merchant_id) is None
    assert repository.list_events(foreign.id, dashboard_user.merchant_id) == []


def test_idempotency_works_with_cookie_auth(anon_client, dashboard_user):
    assert sign_in(anon_client).status_code == 200
    headers = {"Idempotency-Key": "synthetic-replay-key"}
    one = anon_client.post("/api/v1/payments", json=payload(), headers=headers)
    two = anon_client.post("/api/v1/payments", json=payload(), headers=headers)
    conflict = anon_client.post("/api/v1/payments", json=payload("different-order"), headers=headers)
    assert one.status_code == two.status_code == 201
    assert one.json()["id"] == two.json()["id"]
    assert conflict.status_code == 409


def test_cookie_and_bearer_are_not_combined(anon_client, dashboard_user):
    assert sign_in(anon_client).status_code == 200
    assert anon_client.get("/api/v1/payments", headers={"Authorization": "Bearer synthetic-key"}).status_code == 401


def test_secure_cookie_configuration(anon_client, dashboard_user, monkeypatch):
    monkeypatch.setattr(settings, "public_origin", "https://dashboard.example.com")
    anon_client.base_url = "https://testserver"
    response = sign_in(anon_client, origin=settings.public_origin)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "__host-pulsepay_session=" in cookie
    assert "; secure" in cookie and "httponly" in cookie and "path=/" in cookie


def test_production_rejects_http():
    with pytest.raises(ValueError):
        Settings(_env_file=None, app_env="production", public_origin="http://localhost:5173")


def test_seed_is_blocked_before_database_access(monkeypatch):
    from app import seed
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "seed_dev_enabled", True)
    monkeypatch.setattr(seed, "SessionLocal", lambda: pytest.fail("Production seed accessed the database"))
    with pytest.raises(RuntimeError, match="disabled"):
        seed.seed_dev_merchant()


def test_panel_user_provisioning_does_not_issue_api_keys(db_session):
    merchant, key = MerchantService(repository=SqlAlchemyMerchantRepository(db_session)).create_merchant(
        "Panel", issue_api_key=False)
    assert key is None
    assert db_session.scalar(select(MerchantApiKey).where(MerchantApiKey.merchant_id == merchant.id)) is None


def test_revoked_integration_key_is_rejected(client, merchant, db_session):
    key = db_session.scalar(select(MerchantApiKey).where(MerchantApiKey.merchant_id == merchant.id))
    key.revoked_at = datetime.now(UTC)
    db_session.commit()
    assert client.get("/api/v1/payments").status_code == 401


def test_client_cannot_choose_payment_owner(anon_client, dashboard_user, db_session):
    other, _ = MerchantService(repository=SqlAlchemyMerchantRepository(db_session)).create_merchant("Other", issue_api_key=False)
    assert sign_in(anon_client).status_code == 200
    data = payload("owner-injection")
    data["merchant_id"] = str(other.id)
    response = anon_client.post("/api/v1/payments", json=data)
    assert response.status_code == 201
    from uuid import UUID
    assert db_session.get(Payment, UUID(response.json()["id"])).merchant_id == dashboard_user.merchant_id


def test_development_seed_requires_opt_in(monkeypatch):
    from app import seed
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "seed_dev_enabled", False)
    monkeypatch.setattr(seed, "SessionLocal", lambda: pytest.fail("Disabled seed accessed the database"))
    seed.seed_dev_merchant()


def test_development_seed_creates_a_panel_user_without_keys(db_session, monkeypatch, tmp_path):
    from contextlib import nullcontext
    from app import seed
    password_file = tmp_path / "synthetic-password"
    password_file.write_text(PASSWORD, encoding="utf-8")
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "seed_dev_enabled", True)
    monkeypatch.setattr(settings, "dev_user_email", "seed@example.com")
    monkeypatch.setattr(settings, "dev_user_password_file", password_file)
    monkeypatch.setattr(seed, "SessionLocal", lambda: nullcontext(db_session))
    seed.seed_dev_merchant()
    seed.seed_dev_merchant()
    user = db_session.scalar(select(DashboardUser).where(DashboardUser.email == "seed@example.com"))
    assert user is not None
    assert db_session.scalar(select(MerchantApiKey).where(MerchantApiKey.merchant_id == user.merchant_id)) is None


@pytest.mark.parametrize("key", ["x" * 256])
def test_idempotency_key_bounds_are_validated(anon_client, dashboard_user, key):
    assert sign_in(anon_client).status_code == 200
    result = anon_client.post("/api/v1/payments", json=payload(), headers={"Idempotency-Key": key})
    assert result.status_code == 422
    assert anon_client.get("/api/v1/payments").json() == []


def test_production_requires_postgresql():
    config = Settings(_env_file=None, app_env="production", public_origin="https://dashboard.example.com",
                      database_url=SecretStr("sqlite://"))
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        config.get_database_url()


def test_https_scheme_cannot_disable_secure_cookies():
    config = Settings(_env_file=None, app_env="production", public_origin="HTTPS://dashboard.example.com")
    assert config.public_origin == "https://dashboard.example.com"
    assert config.secure_cookies
    assert config.session_cookie_name == "__Host-pulsepay_session"


@pytest.mark.parametrize("origin,method,headers,allowed", [
    (ORIGIN, "POST", "content-type,x-csrf-token,idempotency-key", True),
    ("https://attacker.example.com", "POST", "content-type", False),
    ("null", "POST", "content-type", False),
    ("http://localhost.attacker.example.com:5173", "POST", "content-type", False),
    (ORIGIN, "DELETE", "content-type", False),
    (ORIGIN, "POST", "x-unapproved-header", False),
])
def test_cors_preflight_is_restricted(anon_client, origin, method, headers, allowed):
    response = anon_client.options("/api/v1/payments", headers={
        "Origin": origin, "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    })
    assert response.status_code == (200 if allowed else 400)
    assert response.headers.get("Access-Control-Allow-Origin") == (ORIGIN if origin == ORIGIN else None)
    assert "*" not in response.headers.get("Access-Control-Allow-Headers", "")


def test_argon2id_parameters_and_random_salt(encoded_password):
    from argon2 import Type, extract_parameters
    parameters = extract_parameters(encoded_password)
    assert parameters.type == Type.ID
    assert parameters.memory_cost >= 19456 and parameters.time_cost >= 2
    assert parameters.parallelism >= 1 and parameters.salt_len >= 16 and parameters.hash_len >= 32
    assert hash_password(PASSWORD) != encoded_password


def test_consumed_presession_cannot_be_reused(anon_client, dashboard_user):
    previous_csrf = anon_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    previous = anon_client.cookies.get(settings.session_cookie_name)
    assert sign_in(anon_client).status_code == 200
    current = anon_client.cookies.get(settings.session_cookie_name)
    assert len(current) == 43  # 32 random bytes encoded as unpadded base64url.
    anon_client.cookies.set(settings.session_cookie_name, previous)
    assert anon_client.get("/api/v1/auth/session").status_code == 401
    response = anon_client.post("/api/v1/auth/login", json={"email": dashboard_user.email, "password": PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": previous_csrf})
    assert response.status_code == 401
    anon_client.cookies.set(settings.session_cookie_name, current)
    assert anon_client.get("/api/v1/auth/session").status_code == 200


def test_disable_revokes_all_sessions(anon_client, dashboard_user, db_session):
    assert sign_in(anon_client).status_code == 200
    service = AuthService(SqlAlchemyAuthRepository(db_session), settings)
    service.disable_user(dashboard_user)
    assert db_session.scalar(select(DashboardSession).where(DashboardSession.user_id == dashboard_user.id)) is None
    assert anon_client.get("/api/v1/auth/session").status_code == 401
    result = sign_in(anon_client)
    assert result.status_code == 401 and result.json() == {"detail": "Invalid credentials."}


def test_database_errors_do_not_expose_parameters_or_auth_material(anon_client, dashboard_user, monkeypatch, caplog):
    from sqlalchemy.exc import StatementError
    assert sign_in(anon_client).status_code == 200
    token = anon_client.cookies.get(settings.session_cookie_name)
    def fail_query(self, merchant_id):
        raise StatementError("synthetic-private-error", "SELECT :private", {"private": PASSWORD}, RuntimeError(PASSWORD))
    monkeypatch.setattr(SqlAlchemyPaymentRepository, "list_payments", fail_query)
    response = anon_client.get("/api/v1/payments")
    assert response.status_code == 503
    assert response.json() == {"detail": "The operation could not be completed."}
    for marker in (PASSWORD, token, "synthetic-private-error"):
        assert marker not in response.text and marker not in caplog.text
