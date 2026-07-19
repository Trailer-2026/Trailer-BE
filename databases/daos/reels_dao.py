from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.reels import Reels


def get_by_idx(db: Session, reels_idx: int) -> Reels | None:
    """reels_idx로 단건 조회 (soft-delete 제외)."""
    return db.query(Reels).filter(
        Reels.reels_idx == reels_idx,
        Reels.deleted_at.is_(None),
    ).first()


def create(db: Session, *, travel_idx: int, user_idx: int, url: str, title: str | None) -> Reels:
    """릴스 행 생성 (flush만 — commit은 서비스가)."""
    reels = Reels(travel_idx=travel_idx, user_idx=user_idx, url=url, title=title)
    db.add(reels)
    db.flush()
    return reels


def get_random_reels(db: Session, count: int, exclude_idxs: list[int]) -> list[Reels]:
    """무작위 count개 조회 (soft-delete·exclude_idxs 제외)."""
    query = db.query(Reels).filter(Reels.deleted_at.is_(None))
    if exclude_idxs:
        query = query.filter(Reels.reels_idx.notin_(exclude_idxs))
    return query.order_by(func.random()).limit(count).all()
