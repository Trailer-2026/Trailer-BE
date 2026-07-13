from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, UniqueConstraint

from databases.models.base import BaseModel


class Like(BaseModel):
    """좋아요 — 릴스 또는 댓글 하나에 대한 좋아요 1건.

    reels_idx / comment_idx 중 정확히 하나만 채운다(CHECK 제약). 취소는 소프트 삭제가
    아니라 행 삭제다(유니크 제약과 충돌 + 재좋아요가 흔함).
    """

    # ponytail: LIKE는 SQL 예약어라 테이블명은 likes.
    __tablename__ = "likes"
    __table_args__ = (
        # NULL은 유니크 제약에서 서로 구별되므로, 릴스 좋아요 행(comment_idx=NULL)은
        # uq_like_comment에 걸리지 않는다. 두 제약이 각자 자기 타입만 중복 방지.
        UniqueConstraint("reels_idx", "user_idx", name="uq_like_reels"),
        UniqueConstraint("comment_idx", "user_idx", name="uq_like_comment"),
        CheckConstraint(
            "num_nonnulls(reels_idx, comment_idx) = 1", name="ck_like_target"
        ),
        {"comment": "좋아요 (릴스/댓글)"},
    )

    likes_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 사용자"
    )
    # reels_idx/comment_idx 단독 index는 안 붙인다 — 위 유니크 인덱스의 선행 컬럼이라 이미 커버된다.
    reels_idx = Column(
        Integer,
        ForeignKey("reels.reels_idx"),
        nullable=True,
        comment="FK 릴스 (릴스 좋아요면 세팅)",
    )
    comment_idx = Column(
        Integer,
        ForeignKey("comment.comment_idx"),
        nullable=True,
        comment="FK 댓글 (댓글 좋아요면 세팅)",
    )
