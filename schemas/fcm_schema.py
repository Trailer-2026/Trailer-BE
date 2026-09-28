from pydantic import BaseModel, Field


class FcmTokenRequest(BaseModel):
    # 길이 상한은 fcm_token.token 컬럼(String(255))과 같다 — 넘기면 DB 에러로 500이 난다.
    token: str = Field(
        ..., min_length=1, max_length=255,
        description="앱(FCM SDK)이 발급받은 기기 등록 토큰",
    )


class PushResultResponse(BaseModel):
    sent: int = Field(..., description="발송 성공 건수")
    failed: int = Field(..., description="발송 실패 건수")
