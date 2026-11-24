from datetime import UTC, datetime, timedelta
import secrets

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import delete

from app.core.config import Settings
from app.core.security import dummy_password_hash, hash_api_key, hash_password, verify_password
from app.models.auth import DashboardSession, DashboardUser
from app.repositories.auth_repository import SqlAlchemyAuthRepository


class AuthError(Exception):
    def __init__(self, message="Authentication required.", status_code=401):
        super().__init__(message)
        self.status_code = status_code


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class AuthService:
    def __init__(self, repository: SqlAlchemyAuthRepository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    def _session(self, token: str | None, *, authenticated: bool) -> DashboardSession:
        if not token or not 32 <= len(token) <= 128:
            raise AuthError()
        record = self.repository.get_session(hash_api_key(token))
        now = datetime.now(UTC)
        if (
            record is None
            or utc(record.expires_at) <= now
            or utc(record.last_seen_at) + timedelta(seconds=self.settings.session_idle_seconds) <= now
        ):
            raise AuthError()
        if authenticated and (record.user is None or not record.user.active):
            raise AuthError()
        return record

    @staticmethod
    def check_csrf(record: DashboardSession, csrf: str | None) -> None:
        if not csrf or not secrets.compare_digest(record.csrf_token.encode(), csrf.encode()):
            raise AuthError("Request verification failed.", 403)

    def _create_session(self, user_id=None) -> tuple[str, DashboardSession]:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        lifetime = self.settings.session_absolute_seconds if user_id else 600
        record = DashboardSession(
            token_hash=hash_api_key(token),
            csrf_token=secrets.token_urlsafe(32),
            user_id=user_id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=lifetime),
        )
        self.repository.session.add(record)
        return token, record

    def csrf(self, token: str | None, source: str) -> tuple[str | None, DashboardSession]:
        try:
            record = self._session(token, authenticated=False)
        except AuthError:
            self.repository.session.rollback()
            if not self.repository.consume_attempt(f"bootstrap:{source}", 60, 60):
                raise AuthError("Too many requests. Try again later.", 429)
            self.repository.session.execute(
                delete(DashboardSession).where(DashboardSession.expires_at < datetime.now(UTC))
            )
            token, record = self._create_session()
            self.repository.session.commit()
            return token, record
        self.repository.session.commit()
        return None, record

    def login(self, token: str | None, csrf: str | None, email: str, password: str, source: str):
        email = email.lower()
        source_allowed = self.repository.consume_attempt(f"source:{source}", self.settings.login_source_limit)
        account_allowed = self.repository.consume_attempt(f"account:{email}", self.settings.login_account_limit)
        if not source_allowed or not account_allowed:
            raise AuthError("Too many attempts. Try again later.", 429)
        previous = self._session(token, authenticated=False)
        self.check_csrf(previous, csrf)
        if previous.user_id is not None:
            raise AuthError("Sign out before starting another session.", 409)
        user = self.repository.get_user(email, lock=True)
        encoded = user.password_hash if user is not None else dummy_password_hash
        verified = verify_password(password, encoded)
        if user is None or not user.active or not verified:
            self.repository.session.rollback()
            raise AuthError("Invalid credentials.")
        self.repository.session.delete(previous)
        new_token, record = self._create_session(user.id)
        self.repository.clear_account_attempts(email)
        self.repository.session.commit()
        return new_token, record

    def authenticate(self, token: str | None, csrf: str | None = None, *, unsafe=False) -> DashboardUser:
        record = self._session(token, authenticated=True)
        if unsafe:
            self.check_csrf(record, csrf)
        record.last_seen_at = datetime.now(UTC)
        user = record.user
        self.repository.session.commit()
        return user

    def session_info(self, token: str | None):
        record = self._session(token, authenticated=True)
        record.last_seen_at = datetime.now(UTC)
        self.repository.session.commit()
        return record

    def logout(self, token: str | None, csrf: str | None) -> None:
        record = self._session(token, authenticated=True)
        self.check_csrf(record, csrf)
        self.repository.session.delete(record)
        self.repository.session.commit()

    def create_user(self, merchant_id, email: str, password: str) -> DashboardUser:
        user = DashboardUser(
            merchant_id=merchant_id,
            email=str(TypeAdapter(EmailStr).validate_python(email)).lower(),
            password_hash=hash_password(password),
        )
        self.repository.session.add(user)
        self.repository.session.commit()
        return user

    def set_password(self, user: DashboardUser, password: str) -> None:
        user = self.repository.get_user(user.email, lock=True)
        if user is None:
            raise AuthError()
        user.password_hash = hash_password(password)
        self.repository.revoke_user_sessions(user.id)
        self.repository.session.commit()

    def disable_user(self, user: DashboardUser) -> None:
        user = self.repository.get_user(user.email, lock=True)
        if user is None:
            raise AuthError()
        user.active = False
        self.repository.revoke_user_sessions(user.id)
        self.repository.session.commit()
