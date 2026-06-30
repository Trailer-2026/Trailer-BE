import logging
from datetime import datetime

from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import station_dao
from recommend import pipeline, scoring
from recommend.routing import haversine
from schemas.recommend_schema import Course, RecommendResponse, SearchCriteria
from services import route_service
from utils import tour_place

logger = logging.getLogger(__name__)

# 이 거리(km) 이상 떨어지면 '지역 이동'으로 보고 그 날 새 숙소를 잡는다.
_LODGING_MOVE_KM = 25.0


def recommend_courses(db: Session, criteria: SearchCriteria) -> RecommendResponse:
    """검색 조건으로 AI 코스 후보(A/B/C) + 왕복 기차 경로 + 날짜별 숙소를 생성한다.

    관광지·숙소는 **TourAPI 실시간 호출**(utils.tour_place)로 받는다(공모전 정책).
    역 정보만 DB(station)를 쓴다.
      1) 출발역 검증 → 2) 앵커 결정(도착지 지정 / AI 자동 밀집지역) → 3) 도착역 확정
      → 4) 앵커 반경 추천지 실시간 조회 → 점수화 → 코스 → 5) 거점별 숙소 실시간 →
      6) route_service로 왕복 기차 결합(실패해도 코스 유지).
    """
    origin = station_dao.get_by_idx(db, criteria.origin_station_idx)
    if origin is None:
        raise NotFoundException("출발역을 찾을 수 없습니다.")

    k = _trip_days(criteria.go_date, criteria.back_date)
    anchor, auto, dest = _resolve_anchor(db, criteria, origin)
    if dest is None:
        raise NotFoundException("도착역을 결정할 수 없습니다(운행역 정보 없음).")

    candidates = tour_place.live_places(anchor[0], anchor[1], criteria.themes)
    scored = scoring.score_places(candidates, criteria.themes)
    courses = pipeline.build_courses(scored, criteria, k, anchor)
    _assign_lodgings(courses)

    routes, note = _fetch_routes(db, origin, dest, criteria)
    if not courses:
        note = note or "조건에 맞는 추천지를 찾지 못했습니다(TourAPI 실시간 조회 결과 없음)."

    return RecommendResponse(
        destination_station_idx=dest.station_idx,
        destination_name=dest.station_name,
        auto_selected=auto,
        routes=routes,
        courses=courses,
        note=note,
    )


def _resolve_anchor(db: Session, criteria: SearchCriteria, origin):
    """(앵커 좌표, AI 자동선택 여부, 도착역). 도착지 지정 시 그 역, 미지정 시 실시간 밀집지역."""
    if criteria.dest_station_idx is not None:
        dest = station_dao.get_by_idx(db, criteria.dest_station_idx)
        if dest is None:
            raise NotFoundException("도착역을 찾을 수 없습니다.")
        if dest.latitude is None or dest.longitude is None:
            raise BadRequestException("도착역 좌표가 없어 추천을 생성할 수 없습니다.")
        return (dest.latitude, dest.longitude), False, dest

    anchor = tour_place.pick_dense_anchor(criteria.themes)
    if anchor is None:  # 실시간 조회 실패 시 출발역 인근으로 폴백
        anchor = (origin.latitude or 0.0, origin.longitude or 0.0)
    dest = station_dao.nearest(db, anchor[0], anchor[1], require_nat_code=True)
    return anchor, True, dest


def _assign_lodgings(courses: list[Course]) -> None:
    """코스별로 거점(이동한 지역)마다 가장 가까운 숙소 1곳을 실시간 조회해 그 날 밤 숙소로 배정.

    연속 day 중심이 _LODGING_MOVE_KM 이내면 전날 숙소 유지, 그 이상이면 새 숙소.
    마지막 날(귀가일)은 숙소 없음. 중심 좌표를 반올림 캐시해 중복 호출을 줄인다.
    """
    memo: dict[tuple[float, float], object] = {}

    def near(c: tuple[float, float]):
        key = (round(c[0], 2), round(c[1], 2))
        if key not in memo:
            memo[key] = tour_place.nearest_lodging(c[0], c[1])
        return memo[key]

    for course in courses:
        cur_centroid: tuple[float, float] | None = None
        cur_lodging = None
        last = len(course.days) - 1
        for i, day in enumerate(course.days):
            if i == last:
                day.lodging = None
                continue
            c = _day_centroid(day)
            if c is None:
                day.lodging = cur_lodging
                continue
            moved = cur_centroid is None or haversine(cur_centroid[0], cur_centroid[1], c[0], c[1]) > _LODGING_MOVE_KM
            if moved:
                cur_lodging = near(c)
                cur_centroid = c
            day.lodging = cur_lodging


def _day_centroid(day) -> tuple[float, float] | None:
    pts = [(p.lat, p.lng) for p in day.places if p.lat is not None and p.lng is not None]
    if not pts:
        return None
    return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))


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
    except Exception as e:
        logger.warning("기차 경로 조회 실패: %s", e)
        return [], "기차 경로를 불러오지 못했습니다(코스만 제공)."


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
