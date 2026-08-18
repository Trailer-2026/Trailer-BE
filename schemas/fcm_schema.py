from pydantic import BaseModel, Field


class FcmTokenRequest(BaseModel):
    token: str = Field(..., description="앱(FCM SDK)이 발급받은 기기 등록 토큰")


class PushResultResponse(BaseModel):
    sent: int = Field(..., description="발송 성공 건수")
    failed: int = Field(..., description="발송 실패 건수")
