from typing import Optional
from pydantic import BaseModel


class SocialLoginRequest(BaseModel):
    access_token: str


class GoogleIdTokenRequest(BaseModel):
    id_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    user_idx: int
    provider: str
    email: Optional[str] = None

    class Config:
        from_attributes = True
