from sqlalchemy.orm import Session

from databases.models.reels import Reels


def get_by_idx(db: Session, reels_idx: int) -> Reels | None:
    """reels_idx로 단건 조회 (soft-delete 제외)."""
    return db.query(Reels).filter(
        Reels.reels_idx == reels_idx,
        Reels.deleted_at.is_(None),
    ).first()
