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

def get_by_idx(db: Session, station_idx: int) -> Station | None:
    """station_idx로 역 단건 조회 (soft-delete 제외)."""
    return db.query(Station).filter(
        Station.station_idx == station_idx,
        Station.deleted_at.is_(None),
    ).first()


def major_candidates(db: Session) -> list[Station]:
    """'대도시' 역 후보 목록 — 좌표가 있는 역만. 없으면 빈 목록.

    KTX 정차역(is_ktx)을 우선 고른다. 없으면 nat_code 보유역, 그래도 없으면 전체 역.
    역명 하드코딩 없이 is_ktx 생성 컬럼으로 거점 대도시역을 데이터 기반으로 정의한다.

    **좌표마다 부르지 말고 요청당 한 번만 부른다.** 최근접 판정은 DB가 아니라 파이썬이
    하므로(nearest_of) 좌표가 달라져도 후보 목록은 같다 — 도착지 자동 추천은 시도·부분권
    수만큼(17~50회) 최근접을 구하는데, 그때마다 다시 읽으면 같은 목록을 그 횟수만큼 퍼 온다.
    """
    for cond in (Station.is_ktx.is_(True), Station.nat_code.isnot(None), None):
        q = db.query(Station).filter(
            Station.deleted_at.is_(None),
            Station.latitude.isnot(None),
            Station.longitude.isnot(None),
        )
        if cond is not None:
            q = q.filter(cond)
        rows = q.all()
        if rows:
            return rows
    return []


def nearest_of(candidates: list[Station], lat: float, lng: float) -> Station | None:
    """후보 중 좌표에서 가장 가까운 역. 후보가 비면 None. DB를 건드리지 않는 순수 계산.

    거리는 위·경도 차의 제곱합이다(haversine 아님). 한국 위도에서 경도 1°가 위도 1°보다
    짧아 동서 거리를 과대평가하지만, 후보가 거점역 수십 곳뿐이라 순위가 뒤집히는 일이
    드물어 그대로 둔다 — 바꾸면 기존 추천 결과가 조용히 달라진다.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda s: (s.latitude - lat) ** 2 + (s.longitude - lng) ** 2)


def nearest_major(db: Session, lat: float, lng: float) -> Station | None:
    """좌표에서 가장 가까운 '대도시' 역(KTX 정차). 운행역 결정 실패 시 폴백용.

    후보를 그 자리에서 읽어 최근접을 고르는 단발 창구다. **여러 좌표를 훑는 루프에서는
    쓰지 마라** — major_candidates를 밖에서 한 번 부르고 nearest_of를 돌려야 한다.
    """
    return nearest_of(major_candidates(db), lat, lng)


def get_stations(db: Session, query: str | None = None) -> list[Station]:
    """역 목록을 역명 오름차순으로 조회한다.

    query가 있으면 역명 부분일치(ILIKE)로 필터한다("부산" → "부산역" 매칭).
    soft-delete된 역은 제외한다.
    """
    q = db.query(Station).filter(Station.deleted_at.is_(None))
    if query:
        q = q.filter(Station.station_name.ilike(f"%{query}%"))
    return q.order_by(Station.station_name).all()
