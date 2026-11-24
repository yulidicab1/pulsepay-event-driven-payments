from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, SecretStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: SecretStr = Field(min_length=1, max_length=128)


class SessionInfo(BaseModel):
    email: str
    merchant_id: UUID
    csrf_token: str
