from sqlalchemy.orm import Session
from databases.models.station import Station


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
