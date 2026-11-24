from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.deps import get_current_merchant, get_payment_service
from app.domain import IdempotencyConflictError, PaymentAlreadyExistsError, PaymentNotFoundError
from app.models.payment import Merchant
from app.schemas.processing import PaymentEventItem, PaymentMetrics, PaymentProcessingSnapshot
from app.schemas.payment import PaymentCreate, PaymentDetail, PaymentListItem
from app.services.payment_service import PaymentService

router = APIRouter()


@router.post("", response_model=PaymentDetail, status_code=status.HTTP_201_CREATED)
def create_payment(
    payload: PaymentCreate,
    service: PaymentService = Depends(get_payment_service),
    merchant: Merchant = Depends(get_current_merchant),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> PaymentDetail:
    try:
        return service.create_payment(
            payload,
            merchant_id=merchant.id,
            idempotency_key=idempotency_key,
        )
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
def list_payments(
    service: PaymentService = Depends(get_payment_service),
    merchant: Merchant = Depends(get_current_merchant),
) -> list[PaymentListItem]:
    return service.list_payments(merchant.id)


@router.get("/metrics", response_model=PaymentMetrics)
def get_payment_metrics(
    service: PaymentService = Depends(get_payment_service),
    merchant: Merchant = Depends(get_current_merchant),
) -> PaymentMetrics:
    return service.get_metrics(merchant.id)


@router.get("/{payment_id}/processing", response_model=PaymentProcessingSnapshot)
def get_payment_processing_status(
    payment_id: UUID,
    service: PaymentService = Depends(get_payment_service),
    merchant: Merchant = Depends(get_current_merchant),
) -> PaymentProcessingSnapshot:
    try:
        snapshot = service.get_processing_snapshot(payment_id, merchant.id)
        if snapshot is None:
            raise PaymentNotFoundError(payment_id)
        return snapshot
    except PaymentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{payment_id}/events", response_model=list[PaymentEventItem])
def get_payment_events(
    payment_id: UUID,
    service: PaymentService = Depends(get_payment_service),
    merchant: Merchant = Depends(get_current_merchant),
) -> list[PaymentEventItem]:
    try:
        return service.get_payment_events(payment_id, merchant.id)
    except PaymentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{payment_id}", response_model=PaymentDetail)
def get_payment(
    payment_id: UUID,
    service: PaymentService = Depends(get_payment_service),
    merchant: Merchant = Depends(get_current_merchant),
) -> PaymentDetail:
    try:
        return service.require_payment(payment_id, merchant.id)
    except PaymentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
