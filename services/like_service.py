"""좋아요 서비스 — 릴스/댓글 공용(likes 테이블 하나). 트랜잭션(commit)은 이 레이어가 소유한다.

토글이 아니라 POST(좋아요)/DELETE(취소)로 나눈다 — 재시도해도 상태가 뒤집히지 않는다.
이미 좋아요한 대상에 다시 POST하거나, 안 누른 대상에 DELETE해도 에러 없이 현재 상태를 돌려준다(멱등).
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.exceptions.custom import NotFoundException
from databases.daos import comment_dao, like_dao, reels_dao
from schemas.like_schema import LikeResponse


def like_reels(db: Session, user, reels_idx: int) -> LikeResponse:
    """릴스 좋아요. 이미 눌렀으면 그대로 둔다."""
    _reels_or_404(db, reels_idx)
    _insert_like(db, user.user_idx, reels_idx=reels_idx)
    return LikeResponse(liked=True, like_count=like_dao.count_by_reels(db, reels_idx))


def unlike_reels(db: Session, user, reels_idx: int) -> LikeResponse:
    """릴스 좋아요 취소. 안 눌렀으면 아무것도 안 한다."""
    _reels_or_404(db, reels_idx)
    like = like_dao.get(db, user.user_idx, reels_idx=reels_idx)
    if like is not None:
        like_dao.delete(db, like)
        db.commit()
    return LikeResponse(liked=False, like_count=like_dao.count_by_reels(db, reels_idx))


def like_comment(db: Session, user, comment_idx: int) -> LikeResponse:
    """댓글 좋아요. 이미 눌렀으면 그대로 둔다."""
    _comment_or_404(db, comment_idx)
    _insert_like(db, user.user_idx, comment_idx=comment_idx)
    return LikeResponse(liked=True, like_count=like_dao.count_by_comment(db, comment_idx))


def unlike_comment(db: Session, user, comment_idx: int) -> LikeResponse:
    """댓글 좋아요 취소. 안 눌렀으면 아무것도 안 한다."""
    _comment_or_404(db, comment_idx)
    like = like_dao.get(db, user.user_idx, comment_idx=comment_idx)
    if like is not None:
        like_dao.delete(db, like)
        db.commit()
    return LikeResponse(liked=False, like_count=like_dao.count_by_comment(db, comment_idx))


def _insert_like(db: Session, user_idx: int, **target) -> None:
    """좋아요 행 삽입. 이미 있으면(선조회로 걸리든, 동시 요청과 경합하든) 아무 일도 없다.

    하트 더블탭처럼 두 요청이 동시에 오면 둘 다 '아직 없음'을 보고 INSERT해 유니크 제약에
    걸린다(UniqueViolation → 500). 그건 에러가 아니라 '이미 좋아요됨'이므로 삼키고 성공 처리.
    """
    if like_dao.get(db, user_idx, **target) is not None:
        return
    try:
        like_dao.create(db, user_idx, **target)
        db.commit()
    except IntegrityError:
        db.rollback()  # 경합에서 진 쪽 — 상대가 이미 넣었다


def _reels_or_404(db: Session, reels_idx: int) -> None:
    if reels_dao.get_by_idx(db, reels_idx) is None:
        raise NotFoundException("릴스를 찾을 수 없습니다.")


def _comment_or_404(db: Session, comment_idx: int) -> None:
    if comment_dao.get_by_idx(db, comment_idx) is None:
        raise NotFoundException("댓글을 찾을 수 없습니다.")
