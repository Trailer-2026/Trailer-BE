from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint

from databases.models.base import BaseModel


class TravelLike(BaseModel):
    """여행 좋아요 — 지난 여행 카드의 하트 1건.

    릴스/댓글 좋아요(likes 테이블)와 분리한다. likes는 num_nonnulls(reels_idx,
    comment_idx) = 1 CHECK와 컬럼별 필터가 박혀 있어 세 번째 타깃을 끼우면 기존
    좋아요 조회가 조용히 깨진다.

    취소는 소프트 삭제가 아니라 행 삭제다(유니크 제약과 충돌 + 재좋아요가 흔함) —
    databases/models/like.py와 같은 이유의 의도적 예외.
    """

    __tablename__ = "travel_like"
    __table_args__ = (
        UniqueConstraint("travel_idx", "user_idx", name="uq_travel_like"),
        {"comment": "여행 좋아요"},
    )

    travel_like_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 사용자"
    )
    # travel_idx 단독 index는 안 붙인다 — uq_travel_like 인덱스의 선행 컬럼이라 이미 커버된다.
    travel_idx = Column(
        Integer, ForeignKey("travel.travel_idx"), nullable=False, comment="FK 여행"
    )
