from fastapi import APIRouter, Depends, status

from app.api.deps import get_merchant_service, require_admin_token
from app.core.security import key_display_prefix
from app.schemas.merchant import MerchantCreate, MerchantCreated
from app.services.merchant_service import MerchantService

router = APIRouter()


@router.post(
    "",
    response_model=MerchantCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_merchant(
    payload: MerchantCreate,
    service: MerchantService = Depends(get_merchant_service),
) -> MerchantCreated:
    merchant, raw_key = service.create_merchant(payload.name, issue_api_key=payload.issue_api_key)
    return MerchantCreated(
        id=merchant.id,
        name=merchant.name,
        created_at=merchant.created_at,
        api_key=raw_key,
        key_prefix=key_display_prefix(raw_key) if raw_key else None,
    )
