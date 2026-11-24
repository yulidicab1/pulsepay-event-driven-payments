from datetime import UTC, datetime, timedelta
import hashlib

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.auth import DashboardSession, DashboardUser, LoginThrottle


class SqlAlchemyAuthRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_user(self, email: str, *, lock: bool = False) -> DashboardUser | None:
        statement = select(DashboardUser).where(DashboardUser.email == email)
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_session(self, token_hash: str) -> DashboardSession | None:
        return self.session.scalar(
            select(DashboardSession).where(DashboardSession.token_hash == token_hash).with_for_update()
        )

    def consume_attempt(self, subject: str, limit: int, window_seconds: int = 900) -> bool:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=window_seconds)
        bucket = hashlib.sha256(subject.encode()).hexdigest()
        insert = pg_insert if self.session.get_bind().dialect.name == "postgresql" else sqlite_insert
        expired = LoginThrottle.window_start <= cutoff
        statement = insert(LoginThrottle).values(bucket=bucket, attempts=1, window_start=now)
        statement = statement.on_conflict_do_update(
            index_elements=["bucket"],
            set_={
                "attempts": case((expired, 1), else_=LoginThrottle.attempts + 1),
                "window_start": case((expired, now), else_=LoginThrottle.window_start),
            },
        ).returning(LoginThrottle.attempts)
        attempts = self.session.scalar(statement)
        self.session.execute(delete(LoginThrottle).where(LoginThrottle.window_start < now - timedelta(days=1)))
        self.session.commit()
        return attempts <= limit

    def clear_account_attempts(self, email: str) -> None:
        bucket = hashlib.sha256(f"account:{email}".encode()).hexdigest()
        self.session.execute(delete(LoginThrottle).where(LoginThrottle.bucket == bucket))

    def revoke_user_sessions(self, user_id) -> None:
        self.session.execute(delete(DashboardSession).where(DashboardSession.user_id == user_id))
