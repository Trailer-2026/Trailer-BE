from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.ban_schema import BlockedUser
from services import ban_service

router = APIRouter(prefix="/api/blocks", tags=["Block"])


@router.post(
    "/{user_idx}",
    summary="사용자 차단",
    description="해당 사용자를 차단합니다. 차단하면 그 사용자의 릴스·댓글이 **나에게만** 보이지 않습니다(단방향). "
                "이미 차단한 상대에게 다시 호출해도 에러 없이 성공합니다(멱등).\n\n"
                "- 400: 자기 자신 차단\n"
                "- 404: 사용자 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def block_user(
    user_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ban_service.block_user(db, current_user, user_idx)
    return CommonResponse.success_response("사용자 차단 성공")


@router.delete(
    "/{user_idx}",
    summary="차단 해제",
    description="해당 사용자의 차단을 해제합니다. 차단하지 않은 상대에게 호출해도 에러 없이 성공합니다(멱등).\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def unblock_user(
    user_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ban_service.unblock_user(db, current_user, user_idx)
    return CommonResponse.success_response("차단 해제 성공")


@router.get(
    "",
    summary="차단 목록 조회",
    description="내가 차단한 사용자 목록을 최근 차단순으로 반환합니다.\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[list[BlockedUser]],
)
def list_blocked(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = ban_service.list_blocked(db, current_user)
    return CommonResponse.success_response("차단 목록 조회 성공", data=result)
