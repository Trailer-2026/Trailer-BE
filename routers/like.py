from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.like_schema import LikeResponse
from services import like_service

router = APIRouter(prefix="/api", tags=["Like"])


@router.post(
    "/reels/{reels_idx}/likes",
    summary="릴스 좋아요",
    description="릴스에 좋아요를 누릅니다. 이미 누른 상태에서 다시 호출해도 에러 없이 현재 상태를 반환합니다(멱등).\n\n"
                "- 404: 릴스 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[LikeResponse],
)
def like_reels(
    reels_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = like_service.like_reels(db, current_user, reels_idx)
    return CommonResponse.success_response("릴스 좋아요 성공", data=result)


@router.delete(
    "/reels/{reels_idx}/likes",
    summary="릴스 좋아요 취소",
    description="릴스 좋아요를 취소합니다. 누르지 않은 상태에서 호출해도 에러 없이 현재 상태를 반환합니다(멱등).\n\n"
                "- 404: 릴스 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[LikeResponse],
)
def unlike_reels(
    reels_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = like_service.unlike_reels(db, current_user, reels_idx)
    return CommonResponse.success_response("릴스 좋아요 취소 성공", data=result)


@router.post(
    "/comments/{comment_idx}/likes",
    summary="댓글 좋아요",
    description="댓글(답글 포함)에 좋아요를 누릅니다. 이미 누른 상태에서 다시 호출해도 에러 없이 현재 상태를 반환합니다(멱등).\n\n"
                "- 404: 댓글 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[LikeResponse],
)
def like_comment(
    comment_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = like_service.like_comment(db, current_user, comment_idx)
    return CommonResponse.success_response("댓글 좋아요 성공", data=result)


@router.delete(
    "/comments/{comment_idx}/likes",
    summary="댓글 좋아요 취소",
    description="댓글 좋아요를 취소합니다. 누르지 않은 상태에서 호출해도 에러 없이 현재 상태를 반환합니다(멱등).\n\n"
                "- 404: 댓글 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[LikeResponse],
)
def unlike_comment(
    comment_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = like_service.unlike_comment(db, current_user, comment_idx)
    return CommonResponse.success_response("댓글 좋아요 취소 성공", data=result)
