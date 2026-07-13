from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, UniqueConstraint

from databases.models.base import BaseModel


class Ban(BaseModel):
    """차단 목록 — user_idx가 blocked_user_idx를 차단한 관계 1건.

    단방향이다: 내가 차단하면 그 사람의 릴스·댓글이 나에게만 안 보인다.
    차단 해제는 소프트 삭제가 아니라 행 삭제다(유니크 제약과 충돌 + 재차단이 흔하다 — likes와 동일).
    """

    __tablename__ = "ban"
    __table_args__ = (
        UniqueConstraint("user_idx", "blocked_user_idx", name="uq_ban"),
        CheckConstraint("user_idx <> blocked_user_idx", name="ck_ban_not_self"),
        {"comment": "차단 목록"},
    )

    ban_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 차단한 사람"
    )
    # ERD의 user_idx2 — 같은 user를 두 번 참조해서 붙은 이름이라 의미가 드러나게 바꿨다.
    blocked_user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, comment="FK 차단 당한 사람"
    )
