from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.models.payment import QueueMessageStatus


@dataclass(frozen=True)
class QueueEnvelope:
    event_type: str
    payment_id: UUID
    payload: dict[str, Any]


@dataclass(frozen=True)
class ClaimedMessage:
    id: UUID
    payment_id: UUID
    event_type: str
    payload: dict[str, Any]
    status: QueueMessageStatus
    attempts: int
    available_at: datetime
    created_at: datetime


class QueuePublisher(ABC):
    @abstractmethod
    def publish(self, envelope: QueueEnvelope) -> None:
        raise NotImplementedError


class QueueConsumer(ABC):
    @abstractmethod
    def claim_available(self, *, batch_size: int) -> list[ClaimedMessage]:
        raise NotImplementedError

    @abstractmethod
    def mark_completed(self, message_id: UUID) -> None:
        raise NotImplementedError

    @abstractmethod
    def requeue(self, message_id: UUID, *, available_at: datetime, last_error: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def mark_failed(self, message_id: UUID, *, last_error: str) -> None:
        raise NotImplementedError
