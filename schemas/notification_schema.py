from datetime import datetime

from pydantic import BaseModel, Field

from core.enums import NotificationType


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


class NotificationLogItem(BaseModel):
    """알림 화면에 뜨는 알림 1건."""

    notification_log_idx: int = Field(..., description="알림 PK (읽음 처리·페이징 커서에 사용)", examples=[128])
    type: NotificationType = Field(
        ...,
        description="알림 종류 — TRAVEL_SAVED(일정 추가) | TRAVEL_D1(출발 하루 전) | "
                    "TRAVEL_DELETED(일정 삭제) | TRAIN_D10M(열차 출발 10분 전). "
                    "풍경 알림(SCENERY)은 푸시로만 나가고 이 목록에는 담기지 않습니다",
        examples=["TRAVEL_SAVED"],
    )
    title: str = Field(
        ..., description="알림 종류 라벨 — 일정알림 | 풍경알림 (알림 화면의 칩, OS 푸시 제목)",
        examples=["일정알림"],
    )
    body: str = Field(
        ..., description="알림 본문 (화면에 그대로 표시)",
        examples=["'부산 관광지 코스 여행'의 일정에 추가되었어요"],
    )
    travel_idx: int | None = Field(
        None,
        description="이 알림이 가리키는 여행 PK — 탭하면 열 여행. TRAVEL_DELETED는 이미 삭제된 "
                    "여행이라 열면 404이니 이동시키지 마세요. TRAIN_D10M이 직접 입력 승차권에서 "
                    "나온 경우엔 여행이 없어 null이고 대신 ticket_idx가 채워집니다",
        examples=[42],
    )
    ticket_idx: int | None = Field(
        None,
        description="이 알림이 가리키는 직접 입력 승차권 PK — TRAIN_D10M에서만 채워집니다. "
                    "탭하면 승차권 목록(GET /api/tickets)으로 보내면 됩니다. 추천 코스에서 나온 "
                    "TRAIN_D10M은 travel_idx가 대신 채워지므로 여기가 null입니다",
        examples=[7],
    )
    is_read: bool = Field(..., description="읽음 여부", examples=[False])
    created_at: datetime = Field(..., description="알림 발송 시각(ISO-8601)", examples=["2026-08-03T00:00:00+09:00"])


class NotificationLogListResponse(BaseModel):
    """알림 화면 목록 — 최신순, 커서 페이징."""

    unread_count: int = Field(..., description="안 읽은 알림 수 (뱃지 표시용, 페이징과 무관한 전체 기준)", examples=[3])
    items: list[NotificationLogItem] = Field(..., description="알림 목록 (최신순)")
    next_cursor: int | None = Field(
        None,
        description="다음 페이지 요청 시 cursor로 넘길 값. null이면 마지막 페이지",
        examples=[100],
    )
