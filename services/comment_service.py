"""릴스 댓글 서비스 — 작성/목록/수정/삭제. 트랜잭션(commit)은 이 레이어가 소유한다.

답글은 depth 1로 제한한다(답글의 답글 금지). 트리가 깊어지면 화면도 API도 복잡해지는데
인스타·틱톡류 릴스 댓글은 1단계면 충분하다. 목록은 최상위 댓글 밑에 답글을 접어서 준다.
"""
from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import ban_dao, comment_dao, like_dao, reels_dao
from schemas.comment_schema import CommentResponse


def create_comment(
    db: Session, user, reels_idx: int, content: str, parent_idx: int | None
) -> CommentResponse:
    """댓글(또는 답글) 1건 작성."""
    if reels_dao.get_by_idx(db, reels_idx) is None:
        raise NotFoundException("릴스를 찾을 수 없습니다.")

    if parent_idx is not None:
        parent = comment_dao.get_by_idx(db, parent_idx)
        if parent is None:
            raise NotFoundException("부모 댓글을 찾을 수 없습니다.")
        if parent.reels_idx != reels_idx:
            raise BadRequestException("다른 릴스의 댓글에는 답글을 달 수 없습니다.")
        if parent.parent_idx is not None:
            raise BadRequestException("답글에는 답글을 달 수 없습니다.")

    comment = comment_dao.create(
        db, reels_idx=reels_idx, user_idx=user.user_idx, content=content, parent_idx=parent_idx
    )
    db.commit()
    return _to_response(comment, user.nickname, user.profile_image)


def list_comments(db: Session, user, reels_idx: int) -> list[CommentResponse]:
    """릴스의 댓글 목록 — 최상위 댓글 작성순, 각 댓글의 replies에 답글 작성순.

    좋아요 수·내 좋아요 여부는 댓글 PK 전체를 IN 절로 한 번씩만 조회해 붙인다(N+1 회피).
    내가 차단한 사용자의 댓글은 쿼리 단계에서 빠진다.
    """
    if reels_dao.get_by_idx(db, reels_idx) is None:
        raise NotFoundException("릴스를 찾을 수 없습니다.")

    rows = comment_dao.list_by_reels(
        db, reels_idx, exclude_user_idxs=ban_dao.blocked_user_idxs(db, user.user_idx)
    )
    idxs = [c.comment_idx for c, _ in rows]
    counts = like_dao.counts_by_comments(db, idxs)
    liked = like_dao.liked_comment_idxs(db, user.user_idx, idxs)

    tops: list[CommentResponse] = []
    by_idx: dict[int, CommentResponse] = {}
    for comment, nickname, profile_image in rows:  # comment_idx 오름차순 = 부모가 답글보다 항상 먼저 나온다
        item = _to_response(
            comment, nickname, profile_image,
            like_count=counts.get(comment.comment_idx, 0),
            liked=comment.comment_idx in liked,
        )
        by_idx[comment.comment_idx] = item
        if comment.parent_idx is None:
            tops.append(item)
            continue
        parent = by_idx.get(comment.parent_idx)
        if parent is not None:
            parent.replies.append(item)
        # 부모가 차단으로 걸러졌으면 답글도 같이 숨긴다 — 최상위로 튀어나오면 맥락 없는 댓글이 된다.
    return tops


def update_comment(db: Session, user, comment_idx: int, content: str) -> CommentResponse:
    """댓글 내용 수정 — 본인 댓글만."""
    comment = _own_comment(db, user, comment_idx)
    comment_dao.update_content(db, comment, content)
    db.commit()
    return _to_response(
        comment, user.nickname, user.profile_image,
        like_count=like_dao.count_by_comment(db, comment_idx),
        liked=like_dao.get(db, user.user_idx, comment_idx=comment_idx) is not None,
    )


def delete_comment(db: Session, user, comment_idx: int) -> None:
    """댓글 삭제(소프트) — 본인 댓글만. 답글도 함께 삭제된다."""
    comment = _own_comment(db, user, comment_idx)
    comment_dao.soft_delete(db, comment)
    db.commit()


def _own_comment(db: Session, user, comment_idx: int):
    """본인 댓글 조회. 없으면 404, 남의 댓글이면 404(존재 여부를 흘리지 않는다)."""
    comment = comment_dao.get_by_idx(db, comment_idx)
    if comment is None or comment.user_idx != user.user_idx:
        raise NotFoundException("댓글을 찾을 수 없습니다.")
    return comment


def _to_response(
    comment, nickname: str | None, profile_image: str | None = None,
    like_count: int = 0, liked: bool = False
) -> CommentResponse:
    return CommentResponse(
        comment_idx=comment.comment_idx,
        reels_idx=comment.reels_idx,
        user_idx=comment.user_idx,
        nickname=nickname,
        profile_image=profile_image,
        content=comment.content,
        parent_idx=comment.parent_idx,
        created_at=comment.created_at,
        like_count=like_count,
        liked=liked,
    )
