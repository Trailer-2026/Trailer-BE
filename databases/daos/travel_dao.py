from datetime import date

from sqlalchemy.orm import Session

from databases.models.travel import Travel


def create(
    db: Session, user_idx: int, title: str, start_date: date, end_date: date,
    region: str | None = None, status: str = "PLANNED",
) -> Travel:
    """여행(일정) 1건 생성. flush만 하고 commit은 서비스가 한다."""
    travel = Travel(
        user_idx=user_idx, title=title, start_date=start_date, end_date=end_date,
        region=region, status=status,
    )
    db.add(travel)
    db.flush()
    return travel


def get_by_idx(db: Session, travel_idx: int) -> Travel | None:
    """travel_idx로 단건 조회 (soft-delete 제외)."""
    return db.query(Travel).filter(
        Travel.travel_idx == travel_idx,
        Travel.deleted_at.is_(None),
    ).first()
