from pydantic import BaseModel, Field


class NotificationResponse(BaseModel):
    """알림 설정 화면의 on/off 스위치 상태."""

    event_alarm: bool = Field(..., description="이벤트 알림 수신 여부", examples=[True])
    scenery_alarm: bool = Field(..., description="기차역 풍경 알림 수신 여부", examples=[False])


class NotificationUpdateRequest(BaseModel):
    """알림 설정 편집 — 보낸 항목만 바뀌고, 안 보낸 항목은 그대로 유지된다."""

    event_alarm: bool | None = Field(
        None, description="이벤트 알림 수신 여부 (미포함 시 기존 값 유지)", examples=[True],
    )
    scenery_alarm: bool | None = Field(
        None, description="기차역 풍경 알림 수신 여부 (미포함 시 기존 값 유지)", examples=[False],
    )
