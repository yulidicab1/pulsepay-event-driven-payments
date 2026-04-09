from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "PulsePay API"
    app_env: str = "development"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "postgresql+psycopg://pulsepay:pulsepay@localhost:5432/pulsepay"
    payment_queue_name: str = "payment-events"
    payment_max_retries: int = 3
    payment_retry_delay_seconds: int = 5
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 10
    payment_random_failure_rate: float = 0.0


settings = Settings()
