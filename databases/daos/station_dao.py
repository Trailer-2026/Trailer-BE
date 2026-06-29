import logging

from sqlalchemy.orm import Session

from databases.models.station import Station

logger = logging.getLogger(__name__)


def get_by_idx(db: Session, station_idx: int) -> Station | None:
    """station_idx로 역 단건 조회 (soft-delete 제외)."""
    return db.query(Station).filter(
        Station.station_idx == station_idx,
        Station.deleted_at.is_(None),
    ).first()


def get_stations(db: Session, query: str | None = None) -> list[Station]:
    """역 목록을 역명 오름차순으로 조회한다.

    query가 있으면 역명 부분일치(ILIKE)로 필터한다("부산" → "부산역" 매칭).
    soft-delete된 역은 제외한다.
    """
    q = db.query(Station).filter(Station.deleted_at.is_(None))
    if query:
        q = q.filter(Station.station_name.ilike(f"%{query}%"))
    return q.order_by(Station.station_name).all()
