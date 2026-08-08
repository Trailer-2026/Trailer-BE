from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.notification_schema import NotificationLogListResponse
from services import notification_service

router = APIRouter(prefix="/api/notifications", tags=["알림"])


@router.get(
    "",
    summary="알림 목록 조회",
    description="알림 화면에 뜨는 내 알림을 최신순으로 반환합니다. 여행을 담았을 때, 여행 출발 "
                "하루 전, 여행을 삭제했을 때, 그리고 탑승할 열차가 출발하기 10분 전에 알림이 여기에 "
                "쌓입니다. 열차 출발 알림은 추천 코스 승차권과 직접 입력 승차권 모두에서 나가며, "
                "출발 1건당 한 번만 옵니다. 실시간 창밖 풍경 알림은 푸시로만 나가고 이 목록에는 "
                "포함되지 않습니다 — 풍경은 GET /api/scenic-spots/nearby 응답으로 화면 상단 카드를 "
                "그리면 됩니다. unread_count는 페이징과 무관한 "
                "전체 안 읽은 수(뱃지용)입니다. 다음 페이지는 응답의 next_cursor를 cursor로 넘겨 "
                "요청하고, next_cursor가 null이면 마지막 페이지입니다. (access token 인증 필요)\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[NotificationLogListResponse],
)
def get_notifications(
    limit: int = Query(20, ge=1, le=100, description="한 번에 가져올 개수 (1~100, 기본 20)"),
    cursor: int | None = Query(
        None, ge=1, description="이전 응답의 next_cursor. 첫 페이지는 생략합니다.",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = notification_service.list_logs(db, current_user, limit, cursor)
    return CommonResponse.success_response("알림 목록 조회 성공", data=result)


@router.patch(
    "/read-all",
    summary="알림 전체 읽음 처리",
    description="안 읽은 알림을 모두 읽음 처리합니다. 읽을 알림이 없어도 성공으로 응답합니다(멱등). "
                "(access token 인증 필요)\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def read_all_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notification_service.mark_all_read(db, current_user)
    return CommonResponse.success_response("알림 전체 읽음 처리 성공")


@router.patch(
    "/{notification_log_idx}/read",
    summary="알림 읽음 처리",
    description="알림 1건을 읽음 처리합니다. 이미 읽은 알림이면 읽은 시각을 바꾸지 않습니다(멱등). "
                "(access token 인증 필요)\n\n"
                "- 401: 인증 필요\n"
                "- 404: 알림을 찾을 수 없음 (없거나 내 알림이 아님)",
    response_model=CommonResponse[None],
)
def read_notification(
    notification_log_idx: int = Path(..., ge=1, description="읽음 처리할 알림 PK"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notification_service.mark_read(db, current_user, notification_log_idx)
    return CommonResponse.success_response("알림 읽음 처리 성공")
