import logging
from sqlalchemy.orm import Session
from databases.daos import station_dao, train_stop_dao
from databases.models.scenic_spot import ScenicSpot
from databases.models.scenic_spot_segment import ScenicSpotSegment
from utils.scenic import (
    haversine_m, bearing_deg, angle_diff_deg,
    SCENIC_NATURAL_CATEGORIES, VISIBLE_RADIUS_M, HEADING_TOLERANCE_DEG,
)

logger = logging.getLogger(__name__)


def _suffix(name: str) -> str:
    """'대전' → '대전역'. train_stop 은 접미사가 없고 scenic_spot_segment 는 붙어 있다."""
    return name if name.endswith("역") else f"{name}역"


def resolve_side(seg: ScenicSpotSegment, from_station: str, to_station: str) -> str | None:
    """진행 방향(출발역→도착역)에 맞춰 segment의 좌/우(left|right)를 하나로 확정한다.

    저장된 segment 기준 출발→도착 정방향이면 side_hint_forward, 역방향이면 side_hint_reverse.
    """
    if seg.from_station == from_station and seg.to_station == to_station:
        return seg.side_hint_forward
    return seg.side_hint_reverse  # 프론트에서 역쌍 매칭만 넘어온다 가정


def _side_on_route(seg: ScenicSpotSegment, dist_from_dep: dict[str, float]) -> str | None:
    """출발역에서 더 가까운 쪽이 segment 의 앞이면 정방향, 아니면 역방향.

    resolve_side 는 탑승 구간의 양 끝(서울역→대전역)과 segment 의 역쌍이 같다는 전제라
    경로를 펴서 찾은 segment(광명역→천안아산역 등)에는 쓸 수 없다. 정차역 목록은 여러
    열차를 합친 것이라 순서(index)도 믿을 수 없어서, **출발역으로부터의 거리**로 앞뒤를
    가른다 — 선로가 달라도 진행 방향은 같으므로 이 기준은 흔들리지 않는다.
    """
    a, b = dist_from_dep.get(seg.from_station), dist_from_dep.get(seg.to_station)
    if a is None or b is None:
        return seg.side_hint_forward
    return seg.side_hint_forward if a <= b else seg.side_hint_reverse


def segments_on_route(
    db: Session, station_names: list[str],
) -> list[tuple[ScenicSpotSegment, ScenicSpot]]:
    """양끝이 모두 경로 위에 있는 segment를 관광지와 함께 한 번에 조회한다.

    풍경 알림 시각표(scenic_plan_service)가 '이 열차가 지나는 구간에 뭐가 있나'를 물을 때
    쓴다. search_on_segment가 역쌍 하나를 보는 것과 달리 여기는 **경로 전체**를 한 방에
    받는다 — 정차역이 열 곳이면 구간이 아홉 개라 쌍마다 조회하면 그만큼 왕복한다.

    인접 쌍으로 좁히지 않고 '양끝이 경로에 있으면' 다 가져오는 이유: segment가 어느
    granularity로 등록돼 있는지 보장이 없다. KTX처럼 중간역을 통과하는 열차는 인접 쌍이
    (서울역, 대전역)인데 segment는 (광명역, 천안아산역) 단위로 등록돼 있을 수 있고, 그러면
    인접 쌍 매칭으로는 아무것도 못 찾는다. 호출측이 역별 진행률을 이미 알고 있어 양끝만
    경로에 있으면 위치를 계산할 수 있다.

    자연 카테고리·미삭제만 남기는 필터는 search_on_segment와 같다.
    """
    if len(station_names) < 2:
        return []
    return (
        db.query(ScenicSpotSegment, ScenicSpot)
        .join(ScenicSpot, ScenicSpot.scenic_spot_idx == ScenicSpotSegment.scenic_spot_idx)
        .filter(
            ScenicSpotSegment.deleted_at.is_(None),
            ScenicSpot.deleted_at.is_(None),
            ScenicSpot.category.in_(SCENIC_NATURAL_CATEGORIES),
            ScenicSpotSegment.from_station.in_(station_names),
            ScenicSpotSegment.to_station.in_(station_names),
        )
        .all()
    )


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
    #
    # **양끝 역쌍만으로 찾지 않는다.** segment 는 인접역 단위로 등록돼 있어서(서울역-광명역,
    # 광명역-천안아산역 …) 탑승 구간의 양 끝인 (서울역, 대전역)으로 물으면 **한 건도 안 걸린다**
    # — 중간에 스팟이 341곳 있어도 화면이 "알려드릴 관광지가 없어요"로 비었다.
    # 그래서 경로를 정차역으로 펴서(train_stop) 양끝이 그 위에 있는 segment 를 다 본다.
    # 풍경 알림 발송 경로(scenic_plan_service)가 segments_on_route 로 하던 것과 같은 방식이고,
    # 조회만 그 교훈이 빠져 있어 "푸시는 가는데 화면엔 안 뜨는" 비대칭이 있었다.
    route = [_suffix(n) for n in train_stop_dao.stops_between(db, from_station, to_station)]
    if len(route) < 2:
        route = [from_station, to_station]  # 미적재 열차 — 옛 동작(양끝 쌍)으로 폴백
    # 좌/우 판정 기준: 출발역에서 각 역까지의 거리(정차 순서가 아니다 — 아래 _side_on_route).
    origin = station_dao.coord_by_name(db, from_station)
    dist_from_dep: dict[str, float] = {}
    if origin is not None:
        for name, coord in station_dao.coords_by_names(db, route).items():
            dist_from_dep[name] = haversine_m(origin[0], origin[1], coord[0], coord[1])
    segs = db.query(ScenicSpotSegment).filter(
        ScenicSpotSegment.deleted_at.is_(None),
        ScenicSpotSegment.from_station.in_(route),
        ScenicSpotSegment.to_station.in_(route),
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
    # scenic_spot_idx는 풍경 알림의 중복 발송 판정·딥링크 키다. 응답 스키마
    # (ScenicSpotResponse)에는 없는 필드라 API 응답에는 노출되지 않는다.
    results: list[dict] = []
    for distance_m, spot, seg in matches[:top_n]:
        results.append({
            "scenic_spot_idx": spot.scenic_spot_idx,
            "name": spot.name,
            "category": spot.category,
            "distance_m": round(distance_m, 1),
            "side": _side_on_route(seg, dist_from_dep),
        })
    return results
