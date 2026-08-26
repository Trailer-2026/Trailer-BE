from typing import Optional
from pydantic import BaseModel, Field


class SocialLoginRequest(BaseModel):
    access_token: str = Field(..., description="소셜 제공자(카카오)가 발급한 access token")


class GoogleIdTokenRequest(BaseModel):
    id_token: str = Field(
        ...,
        description="구글이 발급한 OIDC id_token(JWT). aud가 서버의 google_client_id와 일치해야 한다.",
    )


class DemoLoginRequest(BaseModel):
    """Play 스토어 심사용 데모 로그인 요청 (소셜 제공자를 거치지 않는다)."""
    username: str = Field(..., description="데모 계정 아이디 (Play Console '앱 액세스'에 등록한 값)")
    password: str = Field(..., description="데모 계정 비밀번호 (Play Console '앱 액세스'에 등록한 값)")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="로그인/재발급 시 발급된 refresh token(JWT)")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="API 인증용 access token(JWT)")
    refresh_token: str = Field(..., description="access token 재발급용 refresh token(JWT)")
    token_type: str = Field("bearer", description="토큰 타입")


class UserResponse(BaseModel):
    user_idx: int = Field(..., description="사용자 PK")
    provider: str = Field(..., description="소셜 제공자 (google | kakao | demo — demo는 스토어 심사용 계정)")
    email: Optional[str] = Field(None, description="이메일 (동의하지 않았으면 null)")

    class Config:
        from_attributes = True
