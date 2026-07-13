from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.like import Like


def get(
    db: Session, user_idx: int, reels_idx: int | None = None, comment_idx: int | None = None
) -> Like | None:
    """사용자가 그 릴스/댓글에 누른 좋아요 1건. 없으면 None."""
    return (
        db.query(Like)
        .filter(
            Like.user_idx == user_idx,
            Like.reels_idx == reels_idx,
            Like.comment_idx == comment_idx,
        )
        .first()
    )


def create(
    db: Session, user_idx: int, reels_idx: int | None = None, comment_idx: int | None = None
) -> Like:
    """좋아요 1건 생성. flush만 하고 commit은 서비스가 한다."""
    like = Like(user_idx=user_idx, reels_idx=reels_idx, comment_idx=comment_idx)
    db.add(like)
    db.flush()
    return like


def delete(db: Session, like: Like) -> None:
    """좋아요 취소 = 행 삭제(소프트 삭제 아님 — 유니크 제약과 충돌하고 재좋아요가 흔하다)."""
    db.delete(like)
    db.flush()


def count_by_reels(db: Session, reels_idx: int) -> int:
    """릴스의 좋아요 수."""
    return (
        db.query(func.count(Like.likes_idx)).filter(Like.reels_idx == reels_idx).scalar() or 0
    )


def count_by_comment(db: Session, comment_idx: int) -> int:
    """댓글의 좋아요 수."""
    return (
        db.query(func.count(Like.likes_idx)).filter(Like.comment_idx == comment_idx).scalar() or 0
    )


def counts_by_comments(db: Session, comment_idxs: list[int]) -> dict[int, int]:
    """댓글 여러 건의 좋아요 수를 {comment_idx: count}로 일괄 조회 (목록 조회 N+1 회피)."""
    if not comment_idxs:
        return {}
    rows = (
        db.query(Like.comment_idx, func.count(Like.likes_idx))
        .filter(Like.comment_idx.in_(comment_idxs))
        .group_by(Like.comment_idx)
        .all()
    )
    return {comment_idx: count for comment_idx, count in rows}


def liked_comment_idxs(db: Session, user_idx: int, comment_idxs: list[int]) -> set[int]:
    """그 사용자가 좋아요한 댓글 PK 집합 (목록 조회에서 '내가 누른 좋아요' 표시용)."""
    if not comment_idxs:
        return set()
    rows = (
        db.query(Like.comment_idx)
        .filter(Like.user_idx == user_idx, Like.comment_idx.in_(comment_idxs))
        .all()
    )
    return {comment_idx for (comment_idx,) in rows}
