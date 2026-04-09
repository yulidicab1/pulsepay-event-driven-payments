import time

from app.api.deps import build_worker_service
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, log_event
from app.db.base import *  # noqa: F403
from app.db.session import SessionLocal

logger = get_logger(__name__)


def run_worker() -> None:
    configure_logging()
    log_event(
        logger,
        "worker_started",
        queue_name=settings.payment_queue_name,
        batch_size=settings.worker_batch_size,
        poll_interval_seconds=settings.worker_poll_interval_seconds,
    )

    while True:
        with SessionLocal() as session:
            worker_service = build_worker_service(session)
            result = worker_service.process_available_events()
            if result.claimed > 0:
                log_event(
                    logger,
                    "worker_batch_processed",
                    claimed=result.claimed,
                    completed=result.completed,
                    retried=result.retried,
                    failed=result.failed,
                )
        time.sleep(settings.worker_poll_interval_seconds)


if __name__ == "__main__":
    run_worker()
