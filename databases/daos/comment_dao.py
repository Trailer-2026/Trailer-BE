from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.comment import Comment
from databases.models.user import User


def create(
    db: Session, reels_idx: int, user_idx: int, content: str, parent_idx: int | None = None
) -> Comment:
    """댓글 1건 생성. flush만 하고 commit은 서비스가 한다."""
    comment = Comment(
        reels_idx=reels_idx, user_idx=user_idx, content=content, parent_idx=parent_idx
    )
    db.add(comment)
    db.flush()
    return comment


def get_by_idx(db: Session, comment_idx: int) -> Comment | None:
    """comment_idx로 단건 조회 (soft-delete 제외)."""
    return db.query(Comment).filter(
        Comment.comment_idx == comment_idx,
        Comment.deleted_at.is_(None),
    ).first()


def list_by_reels(
    db: Session, reels_idx: int, exclude_user_idxs: list[int] | None = None
) -> list[tuple[Comment, str | None, str | None]]:
    """릴스의 댓글 전체를 작성순으로 (댓글, 작성자 닉네임, 프로필 사진) 튜플로 조회 (soft-delete 제외).

    답글 포함 전량을 닉네임·프로필까지 한 방 조인으로 읽고(N+1 회피) 트리 구성은 서비스가 한다 —
    릴스당 댓글은 많아야 수백 건이라 페이징 없이 단일 쿼리가 가장 싸다.
    exclude_user_idxs(차단한 사용자)의 댓글은 쿼리에서 제외한다.
    """
    q = (
        db.query(Comment, User.nickname, User.profile_image)
        .join(User, User.user_idx == Comment.user_idx)
        .filter(Comment.reels_idx == reels_idx, Comment.deleted_at.is_(None))
    )
    if exclude_user_idxs:
        q = q.filter(Comment.user_idx.notin_(exclude_user_idxs))
    return q.order_by(Comment.comment_idx.asc()).all()


def counts_by_reels(db: Session, reels_idxs: list[int]) -> dict[int, int]:
    """릴스 여러 건의 댓글 수를 {reels_idx: count}로 일괄 조회 (피드 N+1 회피).

    답글(parent_idx 있는 행)도 함께 센다 — 화면의 댓글 수는 "이 릴스에 달린 말"의
    총량이라 목록에 보이는 건수와 일치해야 한다.
    """
    if not reels_idxs:
        return {}
    rows = (
        db.query(Comment.reels_idx, func.count(Comment.comment_idx))
        .filter(Comment.reels_idx.in_(reels_idxs), Comment.deleted_at.is_(None))
        .group_by(Comment.reels_idx)
        .all()
    )
    return {reels_idx: count for reels_idx, count in rows}


def update_content(db: Session, comment: Comment, content: str) -> Comment:
    """댓글 내용 수정. flush만 하고 commit은 서비스가 한다."""
    comment.content = content
    db.flush()
    return comment


def soft_delete(db: Session, comment: Comment) -> None:
    """댓글과 그 답글들을 소프트 삭제 (deleted_at 세팅).

    답글까지 같이 지우는 이유: 부모만 지우면 답글이 트리에서 부모를 잃고 떠돈다.
    답글의 답글은 없으므로(서비스가 depth 1로 제한) 1단계 cascade면 충분하다.
    """
    comment.deleted_at = func.now()
    (
        db.query(Comment)
        .filter(Comment.parent_idx == comment.comment_idx, Comment.deleted_at.is_(None))
        .update({Comment.deleted_at: func.now()}, synchronize_session=False)
    )
    db.flush()
