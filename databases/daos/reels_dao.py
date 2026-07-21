from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.reels import Reels
from databases.models.user import User


def get_by_idx(db: Session, reels_idx: int) -> Reels | None:
    """reels_idx로 단건 조회 (soft-delete 제외)."""
    return db.query(Reels).filter(
        Reels.reels_idx == reels_idx,
        Reels.deleted_at.is_(None),
    ).first()


def create(
    db: Session, *, user_idx: int | None, url: str, title: str | None
) -> Reels:
    """릴스 행 생성 (flush만 — commit은 서비스가)."""
    reels = Reels(user_idx=user_idx, url=url, title=title)
    db.add(reels)
    db.flush()
    return reels


def get_random_reels(
    db: Session, count: int, exclude_idxs: list[int]
) -> list[tuple[Reels, str | None, str | None]]:
    """무작위 count개를 (릴스, 작성자 닉네임, 프로필 사진)으로 조회 (soft-delete·exclude_idxs 제외).

    작성자 없는(사진만 렌더 시절)·탈퇴한 작성자의 릴스도 나오도록 User 는 outer join —
    그런 릴스는 닉네임·프로필이 None (탈퇴 조건은 ON 절에 둬야 릴스가 통째로 빠지지 않는다).
    """
    query = (
        db.query(Reels, User.nickname, User.profile_image)
        .outerjoin(
            User,
            (User.user_idx == Reels.user_idx) & User.deleted_at.is_(None),
        )
        .filter(Reels.deleted_at.is_(None))
    )
    if exclude_idxs:
        query = query.filter(Reels.reels_idx.notin_(exclude_idxs))
    return query.order_by(func.random()).limit(count).all()
