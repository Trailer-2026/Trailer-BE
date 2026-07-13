from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.comment_schema import CommentCreateRequest, CommentResponse, CommentUpdateRequest
from services import comment_service

router = APIRouter(prefix="/api", tags=["Comment"])


@router.post(
    "/reels/{reels_idx}/comments",
    summary="댓글 작성",
    description="릴스에 댓글을 작성합니다. `parent_idx`를 주면 그 댓글의 답글로 달립니다(답글은 1단계까지만).\n\n"
                "- 400: 답글에 답글을 달거나, 다른 릴스의 댓글을 parent_idx로 지정한 경우\n"
                "- 404: 릴스 없음 / 부모 댓글 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[CommentResponse],
)
def create_comment(
    reels_idx: int,
    req: CommentCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = comment_service.create_comment(
        db, current_user, reels_idx, req.content, req.parent_idx
    )
    return CommonResponse.success_response("댓글 작성 성공", data=result)


@router.get(
    "/reels/{reels_idx}/comments",
    summary="댓글 목록 조회",
    description="릴스의 댓글을 작성순으로 반환합니다. 답글은 각 댓글의 `replies`에 담겨 옵니다.\n\n"
                "- 404: 릴스 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[list[CommentResponse]],
)
def list_comments(
    reels_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = comment_service.list_comments(db, current_user, reels_idx)
    return CommonResponse.success_response("댓글 목록 조회 성공", data=result)


@router.patch(
    "/comments/{comment_idx}",
    summary="댓글 수정",
    description="본인이 쓴 댓글의 내용을 수정합니다.\n\n"
                "- 404: 댓글 없음 (남의 댓글도 404)\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[CommentResponse],
)
def update_comment(
    comment_idx: int,
    req: CommentUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = comment_service.update_comment(db, current_user, comment_idx, req.content)
    return CommonResponse.success_response("댓글 수정 성공", data=result)


@router.delete(
    "/comments/{comment_idx}",
    summary="댓글 삭제",
    description="본인이 쓴 댓글을 삭제합니다(소프트 삭제). 그 댓글의 답글도 함께 삭제됩니다.\n\n"
                "- 404: 댓글 없음 (남의 댓글도 404)\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def delete_comment(
    comment_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment_service.delete_comment(db, current_user, comment_idx)
    return CommonResponse.success_response("댓글 삭제 성공")
