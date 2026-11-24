from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def read_secret(value: SecretStr | None, path: Path | None) -> str | None:
    if value is not None and path is not None:
        raise RuntimeError("Configure a secret value or a secret file, exclusively.")
    if path is None:
        return value.get_secret_value() if value is not None else None
    try:
        result = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise RuntimeError("Configured secret file is unavailable.") from None
    if not result:
        raise RuntimeError("Configured secret file is empty.")
    return result


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_name: str = "PulsePay API"
    app_env: str = "development"
    api_v1_prefix: str = "/api/v1"
    database_url: SecretStr | None = None
    database_url_file: Path | None = None
    admin_api_token: SecretStr | None = None
    admin_api_token_file: Path | None = None
    public_origin: str = "http://localhost:5173"
    session_absolute_seconds: int = Field(default=28800, ge=60)
    session_idle_seconds: int = Field(default=1800, ge=60)
    login_account_limit: int = Field(default=5, ge=1)
    login_source_limit: int = Field(default=30, ge=1)
    seed_dev_enabled: bool = False
    dev_user_email: str | None = None
    dev_user_password_file: Path | None = None
    dev_merchant_name: str = "Dev Merchant"
    payment_queue_name: str = "payment-events"
    payment_max_retries: int = 3
    payment_retry_delay_seconds: int = 5
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 10
    payment_random_failure_rate: float = 0.0
    webhook_keyring_file: Path | None = None

    @model_validator(mode="after")
    def validate_origin(self):
        origin = urlsplit(self.public_origin)
        # Accessing port also validates the configured port syntax and range.
        _ = origin.port
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("PUBLIC_ORIGIN must be an exact HTTP(S) origin.")
        if self.app_env not in {"development", "test", "production"}:
            raise ValueError("APP_ENV must be development, test, or production.")
        if self.app_env == "production" and origin.scheme != "https":
            raise ValueError("Production requires an HTTPS public origin.")
        if origin.scheme == "http" and origin.hostname not in {"localhost", "127.0.0.1", "testserver"}:
            raise ValueError("HTTP is supported only for local development.")
        self.public_origin = origin.geturl()
        return self

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.public_origin).scheme == "https"

    @property
    def session_cookie_name(self) -> str:
        return "__Host-pulsepay_session" if self.secure_cookies else "pulsepay_dev_session"

    def get_database_url(self) -> str:
        result = read_secret(self.database_url, self.database_url_file)
        if not result:
            raise RuntimeError("DATABASE_URL or DATABASE_URL_FILE is required.")
        if self.app_env == "production" and not result.startswith(("postgresql://", "postgresql+psycopg://")):
            raise RuntimeError("Production requires PostgreSQL.")
        return result


settings = Settings()
