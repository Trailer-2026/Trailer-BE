import logging
from datetime import datetime

from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import lodging_dao, place_dao, station_dao
from recommend import pipeline, scoring
from recommend.routing import haversine
from recommend.types import ScoredPlace
from schemas.recommend_schema import Course, Lodging, RecommendResponse, SearchCriteria
from services import route_service

logger = logging.getLogger(__name__)

# 앵커(현지 기준점) 반경 확장 단계(km). 충분한 후보가 모일 때까지 넓힌다.
_RADII = [30.0, 60.0, 100.0, 200.0]
# 이 거리(km) 이상 떨어지면 '지역 이동'으로 보고 그 날 새 숙소를 잡는다.
_LODGING_MOVE_KM = 25.0


def recommend_courses(db: Session, criteria: SearchCriteria) -> RecommendResponse:
    """검색 조건으로 AI 코스 후보(A/B/C) + 출발↔도착 왕복 기차 경로를 생성한다.

    1) 출발역 검증 → 2) 테마 후보 점수화 → 3) 앵커(도착지 지정/AI 자동) 결정
    → 4) 도착역 확정(지정 또는 앵커 최근접 운행역) → 5) 반경 풀로 코스 조립
    → 6) route_service로 왕복 기차 경로 결합(실패해도 코스는 유지). 읽기 전용.
    """
    origin = station_dao.get_by_idx(db, criteria.origin_station_idx)
    if origin is None:
        raise NotFoundException("출발역을 찾을 수 없습니다.")

    k = _trip_days(criteria.go_date, criteria.back_date)

    candidates = place_dao.get_candidates(db, criteria.themes)
    scored = scoring.score_places(candidates, criteria.themes)

    anchor, auto = _anchor(db, criteria, scored)
    dest = _arrival_station(db, criteria, anchor)
    if dest is None:
        raise NotFoundException("도착역을 결정할 수 없습니다(운행역 정보 없음).")

    courses = pipeline.build_courses(_within_radius(scored, anchor, k), criteria, k, anchor)
    _assign_lodgings(db, courses)

    routes, note = _fetch_routes(db, origin, dest, criteria)
    if not courses and not scored:
        note = note or "조건에 맞는 추천지를 찾지 못했습니다."

    return RecommendResponse(
        destination_station_idx=dest.station_idx,
        destination_name=dest.station_name,
        auto_selected=auto,
        routes=routes,
        courses=courses,
        note=note,
    )


def _anchor(db: Session, criteria: SearchCriteria, scored: list[ScoredPlace]) -> tuple[tuple[float, float], bool]:
    """(현지 기준점 좌표, AI 자동선택 여부). 도착지 지정 시 그 좌표, 미지정 시 고득점 밀집 지역."""
    if criteria.dest_station_idx is not None:
        dest = station_dao.get_by_idx(db, criteria.dest_station_idx)
        if dest is None:
            raise NotFoundException("도착역을 찾을 수 없습니다.")
        if dest.latitude is None or dest.longitude is None:
            raise BadRequestException("도착역 좌표가 없어 추천을 생성할 수 없습니다.")
        return (dest.latitude, dest.longitude), False
    if not scored:
        # 테마 후보가 없으면 자동선택 불가 → 출발역 인근으로 폴백
        origin = station_dao.get_by_idx(db, criteria.origin_station_idx)
        return (origin.latitude or 0.0, origin.longitude or 0.0), True
    return _densest_center(scored), True


def _arrival_station(db: Session, criteria: SearchCriteria, anchor: tuple[float, float]):
    """도착역: 지정되면 그 역, 아니면 앵커 최근접 운행역(nat_code 보유)."""
    if criteria.dest_station_idx is not None:
        return station_dao.get_by_idx(db, criteria.dest_station_idx)
    return station_dao.nearest(db, anchor[0], anchor[1], require_nat_code=True)


def _fetch_routes(db: Session, origin, dest, criteria: SearchCriteria) -> tuple[list, str | None]:
    """왕복 기차 경로를 붙인다. 같은 역이거나 조회 실패 시 빈 목록 + 안내."""
    if dest.station_idx == origin.station_idx:
        return [], "출발지와 도착지가 같아 기차 구간이 없습니다(현지 여행)."
    try:
        routes = route_service.recommend(
            db, origin.station_idx, dest.station_idx,
            criteria.go_date, criteria.back_date, criteria.go_time, criteria.back_time,
            nail_pass=criteria.use_naeilpass,
        )
        return routes, None
    except Exception as e:  # 기차 API 키 미설정/네트워크 등 — 코스는 유지
        logger.warning("기차 경로 조회 실패: %s", e)
        return [], "기차 경로를 불러오지 못했습니다(코스만 제공)."


def _assign_lodgings(db: Session, courses: list[Course]) -> None:
    """코스별로 거점(이동한 지역)마다 가장 가까운 숙소 1곳을 그 날 밤 숙소로 배정.

    연속 day 중심이 _LODGING_MOVE_KM 이내면 '이동 없음'→전날 숙소 유지,
    그 이상이면 '지역 이동'→그 날 중심 최근접 새 숙소. 마지막 날(귀가일)은 숙소 없음.
    """
    if not courses:
        return
    lodgings = lodging_dao.get_all(db)
    if not lodgings:
        return
    for course in courses:
        cur_centroid: tuple[float, float] | None = None
        cur_lodging: Lodging | None = None
        last = len(course.days) - 1
        for i, day in enumerate(course.days):
            if i == last:  # 귀가일 — 숙박 없음
                day.lodging = None
                continue
            c = _day_centroid(day)
            if c is None:
                day.lodging = cur_lodging
                continue
            moved = cur_centroid is None or haversine(cur_centroid[0], cur_centroid[1], c[0], c[1]) > _LODGING_MOVE_KM
            if moved:
                near = min(lodgings, key=lambda l: (l.lat - c[0]) ** 2 + (l.lng - c[1]) ** 2)
                cur_lodging = Lodging(
                    name=near.name, lodging_type=near.lodging_type, region=near.region,
                    lat=near.lat, lng=near.lng, tel=near.tel, image_url=near.image_url,
                )
                cur_centroid = c
            day.lodging = cur_lodging


def _day_centroid(day) -> tuple[float, float] | None:
    pts = [(p.lat, p.lng) for p in day.places if p.lat is not None and p.lng is not None]
    if not pts:
        return None
    return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))


def _densest_center(scored: list[ScoredPlace], radius: float = 40.0, cap: int = 250) -> tuple[float, float]:
    """고득점 추천지가 가장 밀집한 지점을 앵커로 고른다(AI 목적지 자동 선택, O(cap^2))."""
    top = scored[:cap]
    best, best_sum = top[0], -1.0
    for c in top:
        s = sum(p.score for p in top if haversine(c.lat, c.lng, p.lat, p.lng) <= radius)
        if s > best_sum:
            best_sum, best = s, c
    return (best.lat, best.lng)


def _within_radius(scored: list[ScoredPlace], anchor: tuple[float, float], k: int) -> list[ScoredPlace]:
    """앵커 반경 내 후보. 코스 3개×일수×하루3곳 대비 충분해질 때까지 반경을 넓힌다."""
    need = max(k * 9, 12)
    pool: list[ScoredPlace] = []
    for r in _RADII:
        pool = [p for p in scored if haversine(anchor[0], anchor[1], p.lat, p.lng) <= r]
        if len(pool) >= need:
            break
    return pool


def _trip_days(go_date: str, back_date: str) -> int:
    """가는날~오는날 일수(k). 형식 오류·역순이면 BadRequest."""
    try:
        go = datetime.strptime(go_date, "%Y%m%d")
        back = datetime.strptime(back_date, "%Y%m%d")
    except (TypeError, ValueError):
        raise BadRequestException("날짜 형식이 올바르지 않습니다 (YYYYMMDD).")
    days = (back - go).days + 1
    if days < 1:
        raise BadRequestException("오는날이 가는날보다 빠릅니다.")
    return days
