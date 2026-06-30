import logging

from sqlalchemy.orm import Session

from databases.models.station import Station

logger = logging.getLogger(__name__)

def coord_by_name(db: Session, station_name: str) -> tuple[float, float] | None:
    """역명으로 (위도, 경도)를 조회한다. 없거나 좌표 미등록이면 None.

    station 테이블·segment·API 모두 '대전역'처럼 '역' 접미사를 포함한 동일 형식이라
    역명을 그대로 매칭한다.
    """
    row = (
        db.query(Station.latitude, Station.longitude)
        .filter(
            Station.deleted_at.is_(None),
            Station.station_name == station_name,
        )
        .first()
    )
    if row is None or row.latitude is None or row.longitude is None:
        return None
    return (row.latitude, row.longitude)

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
