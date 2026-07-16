from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.deps import get_payment_service
from app.domain import IdempotencyConflictError, PaymentAlreadyExistsError, PaymentNotFoundError
from app.schemas.processing import PaymentEventItem, PaymentMetrics, PaymentProcessingSnapshot
from app.schemas.payment import PaymentCreate, PaymentDetail, PaymentListItem
from app.services.payment_service import PaymentService

router = APIRouter()


@router.post("", response_model=PaymentDetail, status_code=status.HTTP_201_CREATED)
def create_payment(
    payload: PaymentCreate,
    service: PaymentService = Depends(get_payment_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentDetail:
    try:
        return service.create_payment(payload, idempotency_key=idempotency_key)
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except PaymentAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get("", response_model=list[PaymentListItem])
def list_payments(service: PaymentService = Depends(get_payment_service)) -> list[PaymentListItem]:
    return service.list_payments()


@router.get("/metrics", response_model=PaymentMetrics)
def get_payment_metrics(service: PaymentService = Depends(get_payment_service)) -> PaymentMetrics:
    return service.get_metrics()


@router.get("/{payment_id}/processing", response_model=PaymentProcessingSnapshot)
def get_payment_processing_status(
    payment_id: UUID,
    service: PaymentService = Depends(get_payment_service),
) -> PaymentProcessingSnapshot:
    try:
        snapshot = service.get_processing_snapshot(payment_id)
        if snapshot is None:
            raise PaymentNotFoundError(payment_id)
        return snapshot
    except PaymentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{payment_id}/events", response_model=list[PaymentEventItem])
def get_payment_events(
    payment_id: UUID,
    service: PaymentService = Depends(get_payment_service),
) -> list[PaymentEventItem]:
    try:
        return service.get_payment_events(payment_id)
    except PaymentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{payment_id}", response_model=PaymentDetail)
def get_payment(
    payment_id: UUID,
    service: PaymentService = Depends(get_payment_service),
) -> PaymentDetail:
    try:
        return service.require_payment(payment_id)
    except PaymentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
