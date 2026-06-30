import logging
from datetime import datetime

from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import station_dao
from recommend import destination, pipeline, scoring
from recommend.routing import haversine
from schemas.recommend_schema import (
    Course,
    DestinationPlan,
    RecommendResponse,
    SearchCriteria,
)
from services import route_service
from utils import tour_place

logger = logging.getLogger(__name__)

# 이 거리(km) 이상 떨어지면 '지역 이동'으로 보고 그 날 새 숙소를 잡는다.
_LODGING_MOVE_KM = 25.0

# 도착역 자동 선택 2단계 중 Phase A(거친 시도 선별)에서 정밀 재점수할 후보 수.
_SHORTLIST_N = 5


def recommend_courses(db: Session, criteria: SearchCriteria) -> RecommendResponse:
    """검색 조건으로 AI 코스 + 왕복 기차 경로 + 날짜별 숙소를 생성한다.

    관광지·숙소는 **TourAPI 실시간 호출**(utils.tour_place)로 받는다(공모전 정책).
    역 정보만 DB(station)를 쓴다. 출발역 검증 후 도착역 지정 여부로 분기한다.

    - **도착역 지정**: 그 지역 기준 코스 후보 A/B/C + 왕복 기차 경로(단일 도착지).
    - **도착역 미지정**: theme+party(여행 인원 수) 기준으로 서로 다른 권역의 도착지 후보 최대 3곳을
      선정(recommend.destination)하고, 후보마다 코스 1개 + 경로를 만든다.
    """
    origin = station_dao.get_by_idx(db, criteria.origin_station_idx)
    if origin is None:
        raise NotFoundException("출발역을 찾을 수 없습니다.")

    k = _trip_days(criteria.go_date, criteria.back_date)
    if criteria.dest_station_idx is not None:
        return _recommend_fixed_dest(db, criteria, origin, k)
    return _recommend_auto_dest(db, criteria, origin, k)


def _recommend_fixed_dest(db, criteria, origin, k) -> RecommendResponse:
    """도착역 지정 — 그 지역 기준 코스 후보 A/B/C + 왕복 기차 경로."""
    dest = station_dao.get_by_idx(db, criteria.dest_station_idx)
    if dest is None:
        raise NotFoundException("도착역을 찾을 수 없습니다.")
    if dest.latitude is None or dest.longitude is None:
        raise BadRequestException("도착역 좌표가 없어 추천을 생성할 수 없습니다.")

    courses = _build_courses_at(criteria, k, (dest.latitude, dest.longitude))
    routes, note = _fetch_routes(db, origin, dest, criteria)
    if not courses:
        note = note or "조건에 맞는 추천지를 찾지 못했습니다(TourAPI 실시간 조회 결과 없음)."
    plan = DestinationPlan(
        destination_station_idx=dest.station_idx,
        destination_name=dest.station_name,
        score=None,
        routes=routes,
        courses=courses,
        note=note,
    )
    return RecommendResponse(auto_selected=False, destinations=[plan], note=None)


def _recommend_auto_dest(db, criteria, origin, k) -> RecommendResponse:
    """도착역 미지정 — theme+party 기준으로 도착지 후보 최대 3곳을 골라 각각 코스 1개.

    순위·도착역·코스를 모두 '도착역 주변' 기준으로 일치시키기 위해 2단계로 점수화한다.
      Phase A: 시도(area) 스캔으로 거칠게 상위 N개 후보를 추린다(싸다).
      Phase B: 후보별 도착역 주변을 실측 스캔해 그 분포로 재점수화→top-3. 스캔 결과는 코스 생성에 재사용.
    """
    origin_coords = (origin.latitude or 0.0, origin.longitude or 0.0)

    # Phase A: 시도 스캔 → 역 매핑 → 철도필터(제주 등 제외) → 거친 순위 상위 N
    coarse = []
    for s in tour_place.scan_area_profiles(criteria.themes):
        st = station_dao.nearest_major(db, s.centroid[0], s.centroid[1])
        if st is None or st.latitude is None or st.longitude is None:
            continue
        # 도착역이 지역 중심에서 너무 멀면 철도로 닿기 어려운 곳(바다 건너 제주 등) → 제외
        if haversine(s.centroid[0], s.centroid[1], st.latitude, st.longitude) > destination.MAX_STATION_GAP_KM:
            continue
        coarse.append(destination.AreaProfile(
            area_code=s.area_code,
            centroid=s.centroid,
            theme_counts=s.theme_counts,
            total=s.total,
            station=st,
            province=(getattr(st.region, "value", st.region) if st.region else None),
        ))

    shortlist = _dedup_by_station(destination.rank_and_diversify(
        coarse, criteria.themes, criteria.party, origin_coords, k,
        max_travel_minutes=criteria.max_travel_minutes, top_k=_SHORTLIST_N,
    ))
    if not shortlist:
        return _auto_fallback(db, criteria, origin, origin_coords, k)

    # Phase B: 후보별 도착역 주변 실측 → 재점수 입력 + 코스용 장소를 동시에 확보(캐시)
    refined, place_cache = [], {}
    for p in shortlist:
        st = p.station
        places = tour_place.live_places(st.latitude, st.longitude, criteria.themes)
        place_cache[st.station_idx] = places
        refined.append(destination.AreaProfile(
            area_code=p.area_code,
            centroid=(st.latitude, st.longitude),
            theme_counts=_theme_counts(places),
            total=len(places),
            station=st,
            province=p.province,
        ))

    final = destination.rank_and_diversify(
        refined, criteria.themes, criteria.party, origin_coords, k,
        max_travel_minutes=criteria.max_travel_minutes, top_k=3,
    )
    if not final:
        return _auto_fallback(db, criteria, origin, origin_coords, k)

    plans = []
    for p in final:
        st = p.station
        # 도착역 좌표를 앵커로, Phase B에서 받아둔 장소를 재사용해 코스 1개 생성
        courses = _build_courses_from(
            place_cache[st.station_idx], criteria, k, (st.latitude, st.longitude)
        )[:1]
        routes, rnote = _fetch_routes(db, origin, st, criteria)
        plans.append(DestinationPlan(
            destination_station_idx=st.station_idx,
            destination_name=st.station_name,
            score=round(p.score, 4),
            routes=routes,
            courses=courses,
            note=rnote,
        ))
    return RecommendResponse(auto_selected=True, destinations=plans, note=None)


def _theme_counts(places) -> dict:
    """LivePlace 목록 → 테마별 카운트(themeVector)."""
    counts: dict = {}
    for pl in places:
        for t in pl.themes:
            counts[t] = counts.get(t, 0) + 1
    return counts


def _dedup_by_station(profiles: list) -> list:
    """같은 도착역으로 매핑된 후보를 제거(역 idx 기준)."""
    seen, out = set(), []
    for p in profiles:
        if p.station.station_idx in seen:
            continue
        seen.add(p.station.station_idx)
        out.append(p)
    return out


def _auto_fallback(db, criteria, origin, origin_coords, k) -> RecommendResponse:
    """후보를 전혀 못 찾았을 때: 출발역 인근 대도시(KTX) 한 곳으로 폴백."""
    st = station_dao.nearest_major(db, origin_coords[0], origin_coords[1])
    if st is None:
        return RecommendResponse(
            auto_selected=True, destinations=[],
            note="추천 가능한 도착지를 찾지 못했습니다.",
        )
    courses = _build_courses_at(criteria, k, (st.latitude, st.longitude))[:1]
    routes, rnote = _fetch_routes(db, origin, st, criteria)
    plan = DestinationPlan(
        destination_station_idx=st.station_idx,
        destination_name=st.station_name,
        score=None,
        routes=routes,
        courses=courses,
        note=rnote or "테마 조건에 맞는 도착지 후보를 찾지 못해 인근 대도시를 추천했습니다.",
    )
    return RecommendResponse(auto_selected=True, destinations=[plan], note=None)


def _build_courses_at(criteria: SearchCriteria, k: int, anchor) -> list[Course]:
    """현지 기준점(anchor) 반경 추천지를 실시간 조회 후 코스 생성(지정/폴백 경로용)."""
    places = tour_place.live_places(anchor[0], anchor[1], criteria.themes)
    return _build_courses_from(places, criteria, k, anchor)


def _build_courses_from(places, criteria: SearchCriteria, k: int, anchor) -> list[Course]:
    """이미 받아둔 추천지(places)로 점수화→코스 생성→숙소 배정(중복 조회 회피)."""
    scored = scoring.score_places(places, criteria.themes)
    courses = pipeline.build_courses(scored, criteria, k, anchor)
    _assign_lodgings(courses)
    return courses


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
