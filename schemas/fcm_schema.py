from pydantic import BaseModel, Field


class FcmTokenRequest(BaseModel):
    token: str = Field(..., description="앱(FCM SDK)이 발급받은 기기 등록 토큰")


class TestPushRequest(BaseModel):
    user_idx: int = Field(..., description="푸시를 받을 사용자 PK")
    title: str = Field(..., description="알림 제목")
    body: str = Field(..., description="알림 본문")


class PushResultResponse(BaseModel):
    sent: int = Field(..., description="발송 성공 건수")
    failed: int = Field(..., description="발송 실패 건수")
