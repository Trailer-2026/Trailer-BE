from datetime import datetime

from pydantic import BaseModel, Field


class NotificationItem(BaseModel):
    """알림 리스트의 한 항목."""

    notification_idx: int = Field(..., description="알림 PK")
    type: str = Field(..., description="알림 종류 (예: TRAVEL_ADDED)")
    message: str = Field(..., description="표시 문구 (예: '부산 2박 3일 여행'이 일정에 추가되었어요)")
    created_at: datetime = Field(..., description="생성 시각. 상대시간('14분 전')은 프론트가 계산")


class NotificationListResponse(BaseModel):
    """알림 목록 — 최신순."""

    items: list[NotificationItem] = Field(..., description="알림 항목 (최신순). 없으면 빈 배열")
