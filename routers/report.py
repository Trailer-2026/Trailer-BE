"""사용자 신고 — 동작은 차단(`/api/blocks`)과 같고 URL만 다르다.

앱 화면의 '신고'가 차단 API를 그대로 호출하면 스토어 심사에서 지적받을 수 있어
엔드포인트를 따로 뒀다. 로직은 `ban_service`가 공유한다.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from services import ban_service

router = APIRouter(prefix="/api/reports", tags=["Report"])


@router.post(
    "/{user_idx}",
    summary="사용자 신고",
    description="해당 사용자를 신고합니다. 신고하면 그 사용자의 릴스·댓글이 **나에게만** 보이지 않습니다(단방향). "
                "이미 신고한 상대에게 다시 호출해도 에러 없이 성공합니다(멱등).\n\n"
                "- 400: 자기 자신 신고\n"
                "- 404: 사용자 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def report_user(
    user_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ban_service.report_user(db, current_user, user_idx)
    return CommonResponse.success_response("사용자 신고 성공")
