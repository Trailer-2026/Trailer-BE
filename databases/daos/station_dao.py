from sqlalchemy.orm import Session
from databases.models.station import Station


def coord_by_name(db: Session, station_name: str) -> tuple[float, float] | None:
    """역명으로 (위도, 경도)를 조회한다. 없거나 좌표 미등록이면 None.

    station 테이블은 '대전역'처럼 '역' 접미사를 포함해 저장하는데 호출 측(segment/API)은
    '대전'처럼 접미사 없이 넘기므로, 접미사 유무를 모두 후보로 두고 매칭한다.
    """
    base = station_name[:-1] if station_name.endswith("역") else station_name
    candidates = {station_name, base, base + "역"}
    row = (
        db.query(Station.latitude, Station.longitude)
        .filter(
            Station.deleted_at.is_(None),
            Station.station_name.in_(candidates),
        )
        .first()
    )
    if row is None or row.latitude is None or row.longitude is None:
        return None
    return (row.latitude, row.longitude)
