import os
import json
import logging
from sqlalchemy.orm import Session

from databases.daos import scenic_spot_dao, scenic_spot_segment_dao
from utils.scenic import SCENIC_NATURAL_CATEGORIES

logger = logging.getLogger(__name__)

# 정제된 관광지 시드 JSON (Trailer-BE/data/)
_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "scenic_spots_with_side.json",
)


def find_nearby(db: Session, lat: float, lng: float, from_station: str, to_station: str):
    """출발역→도착역 구간에서 보이는 관광지를 거리순 top3로 반환한다.

    진행 방향에 맞춰 창밖 좌/우(side)를 하나로 확정해 매핑한다.
    """
    items = scenic_spot_dao.search_on_segment(
        db, lat, lng, from_station, to_station, top_n=3,
    )

    return {
        "feature_count": len(items),
        "items": items,
    }


def _load_natural_spots() -> list[dict]:
    """시드 JSON에서 자연 카테고리(water/waterway/peak/natural_view) 스팟만 추린다."""
    with open(_JSON_PATH, encoding="utf-8") as f:
        cache = json.load(f)
    spots = cache.get("spots", [])
    return [s for s in spots if s.get("category") in SCENIC_NATURAL_CATEGORIES]


def seed_if_empty(db: Session) -> None:
    """scenic_spot 테이블이 비어 있으면 시드 JSON을 적재한다. (앱 시작 시 1회)

    segment는 osm_uid가 UNIQUE인 점을 이용한 2-pass bulk로 적재한다:
    관광지를 한 번에 적재 → osm_uid→idx 매핑 확보 → segment를 한 번에 적재.
    """
    if scenic_spot_dao.count(db) > 0:
        return

    try:
        spots = _load_natural_spots()
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("관광지 시드 JSON을 읽을 수 없어 시드를 건너뜁니다: %s", exc)
        return

    # lat/lng 결측 스팟은 제외 (두 패스에서 동일 기준 사용)
    valid_spots = [s for s in spots if s.get("lat") is not None and s.get("lng") is not None]

    # 1-pass: 관광지 일괄 적재
    spot_mappings = [
        {
            "osm_uid": s["osm_uid"],
            "category": s.get("category"),
            "name": s.get("name"),
            "lat": s["lat"],
            "lng": s["lng"],
        }
        for s in valid_spots
    ]
    scenic_spot_dao.bulk_insert(db, spot_mappings)

    # osm_uid → scenic_spot_idx 매핑으로 segment FK 연결
    idx_by_uid = scenic_spot_dao.idx_by_osm_uid(db)

    # 2-pass: 노선 구간(좌/우 창밖 안내) 일괄 적재 (필수 역 누락 segment는 건너뜀)
    segment_mappings: list[dict] = []
    for s in valid_spots:
        spot_idx = idx_by_uid.get(s["osm_uid"])
        if spot_idx is None:
            continue
        for seg in s.get("segments") or []:
            if not (seg.get("from_station") and seg.get("to_station")):
                continue
            segment_mappings.append({
                "scenic_spot_idx": spot_idx,
                "from_station": seg.get("from_station"),
                "to_station": seg.get("to_station"),
                "side_hint_forward": seg.get("side_hint_forward"),
                "side_hint_reverse": seg.get("side_hint_reverse"),
            })
    scenic_spot_segment_dao.bulk_insert(db, segment_mappings)

    db.commit()
    logger.info(
        "관광지 시드 완료: 관광지 %d개 / 구간 %d개 적재",
        len(spot_mappings), len(segment_mappings),
    )
