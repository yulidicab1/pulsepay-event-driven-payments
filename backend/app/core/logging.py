import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **{key: _serialize_value(value) for key, value in fields.items()},
    }
    logger.info(json.dumps(payload, sort_keys=True))


def _serialize_value(value: object) -> object:
    if isinstance(value, (datetime, UUID, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value

