from sqlalchemy.orm import Session

from databases.daos import scenic_spot_dao
from utils.timezone import now_kst


def find_nearby(db: Session, lat: float, lng: float, from_station: str, to_station: str):
    """출발역→도착역 구간에서 보이는 관광지를 거리순 top3로 반환한다.

    진행 방향에 맞춰 창밖 좌/우(side)를 하나로 확정해 매핑한다.
    """
    based_at = now_kst()
    items = scenic_spot_dao.search_on_segment(
        db, lat, lng, from_station, to_station, top_n=3,
    )

    return {
        "based_at": based_at,
        "feature_count": len(items),
        "items": items,
    }
