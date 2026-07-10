from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.notification_schema import NotificationListResponse
from services import notification_service

router = APIRouter(prefix="/api/notifications", tags=["Notification"])


@router.get(
    "",
    summary="알림 목록 조회",
    description="로그인한 사용자의 알림을 최신순으로 반환합니다(알림 화면 하단 리스트). "
                "현재는 여행(일정) 저장 시 '...일정에 추가되었어요' 알림이 쌓입니다. "
                "상대시간('14분 전')은 `created_at`으로 프론트가 계산합니다. "
                "알림이 없으면 items는 빈 배열입니다.\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[NotificationListResponse],
)
def get_notifications(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = notification_service.list_for_user(db, current_user)
    return CommonResponse.success_response("알림 목록 조회 성공", data=result)
