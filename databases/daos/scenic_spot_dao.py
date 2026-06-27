import logging
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from databases.daos import station_dao
from databases.models.scenic_spot import ScenicSpot
from databases.models.scenic_spot_segment import ScenicSpotSegment
from utils.scenic import (
    haversine_m, bearing_deg, angle_diff_deg,
    SCENIC_NATURAL_CATEGORIES, VISIBLE_RADIUS_M, HEADING_TOLERANCE_DEG,
)

logger = logging.getLogger(__name__)


def _resolve_side(seg: ScenicSpotSegment, from_station: str, to_station: str) -> str | None:
    """진행 방향(출발역→도착역)에 맞춰 segment의 좌/우(left|right)를 하나로 확정한다.

    저장된 segment 기준 출발→도착 정방향이면 side_hint_forward, 역방향이면 side_hint_reverse.
    """
    if seg.from_station == from_station and seg.to_station == to_station:
        return seg.side_hint_forward
    return seg.side_hint_reverse  # 프론트에서 역쌍 매칭만 넘어온다 가정


def search_on_segment(
    db: Session, lat: float, lng: float,
    from_station: str, to_station: str, top_n: int = 3,
) -> list[dict]:
    """출발역→도착역 구간에서 보이는 자연 관광지를 거리순 top_n개 반환.

    segment를 1차 필터로 잡고(출발/도착역 양방향 매칭 → 진행 방향 좌/우 확정), 창밖에서 실제로
    보이는 후보만 남긴다: 현재 좌표 기준 haversine 거리 VISIBLE_RADIUS_M(가시 범위) 이내 +
    진행 방향(도착역 방위) 기준 HEADING_TOLERANCE_DEG 이내(앞~옆, 이미 지나간 뒤편 제외).
    남은 후보를 거리순 top_n개로 매핑한다.
    좌/우는 노선과 무관한 기하 속성이라 노선 구분 없이 출발/도착역만으로 방향을 판별한다.
    """
    # segment = '관광지가 어느 역 구간에서 어느 쪽 창으로 보이는가'(정의: ScenicSpotSegment 모델 참조).
    # (출발,도착)역 양방향 매칭 segment 조회 → 진행 방향 좌/우 확정
    segs = db.query(ScenicSpotSegment).filter(
        ScenicSpotSegment.deleted_at.is_(None),
        or_(
            and_(
                ScenicSpotSegment.from_station == from_station,
                ScenicSpotSegment.to_station == to_station,
            ),
            and_(
                ScenicSpotSegment.from_station == to_station,
                ScenicSpotSegment.to_station == from_station,
            ),
        ),
    ).all()
    if not segs:
        return []

    # 관광지별 segment 1개 채택 (같은 역쌍이 여러 노선에 걸려도 좌/우는 동일하므로 첫 매칭)
    side_by_spot: dict[int, ScenicSpotSegment] = {}
    for seg in segs:
        side_by_spot.setdefault(seg.scenic_spot_idx, seg)

    # 채택된 segment의 관광지만 일괄 조회 (자연 카테고리 + 미삭제)
    spots = db.query(ScenicSpot).filter(
        ScenicSpot.deleted_at.is_(None),
        ScenicSpot.category.in_(SCENIC_NATURAL_CATEGORIES),
        ScenicSpot.scenic_spot_idx.in_(side_by_spot.keys()),
    ).all()

    # 진행 방향(heading): "도착역 = 내 앞"으로 보고 현재→도착역 방위각으로 잡는다.
    # 도착역 좌표가 없으면 heading=None → 방향 필터는 생략하고 거리 필터만 적용(fallback).
    dest = station_dao.coord_by_name(db, to_station)
    heading = bearing_deg(lat, lng, dest[0], dest[1]) if dest else None
    if dest is None:
        logger.warning("역 좌표 없음, heading 필터 생략 (거리만 적용): %s", to_station)

    # 창밖에서 실제로 보이는 후보만 통과: 가시 거리 이내 + 진행 방향 앞~옆
    matches: list[tuple[float, ScenicSpot, ScenicSpotSegment]] = []
    for spot in spots:
        distance_m = haversine_m(lat, lng, spot.lat, spot.lng)
        if distance_m > VISIBLE_RADIUS_M:
            continue  # 가시 범위 밖
        if heading is not None and (
            angle_diff_deg(bearing_deg(lat, lng, spot.lat, spot.lng), heading)
            > HEADING_TOLERANCE_DEG
        ):
            continue  # 진행 방향 뒤편 = 이미 지나감
        matches.append((distance_m, spot, side_by_spot[spot.scenic_spot_idx]))

    # 가까운 순 정렬 후 아래에서 top_n개만 사용
    matches.sort(key=lambda m: m[0])

    # 응답에 필요한 필드만 담은 슬림 item으로 매핑 (side는 진행 방향 좌/우 확정)
    results: list[dict] = []
    for distance_m, spot, seg in matches[:top_n]:
        results.append({
            "name": spot.name,
            "category": spot.category,
            "distance_m": round(distance_m, 1),
            "side": _resolve_side(seg, from_station, to_station),
        })
    return results
