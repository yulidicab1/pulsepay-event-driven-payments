from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class MerchantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    issue_api_key: bool = False


class MerchantCreated(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    # The plaintext API key is returned only once, at creation time.
    api_key: str | None
    key_prefix: str | None
