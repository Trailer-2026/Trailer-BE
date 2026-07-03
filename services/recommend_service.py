import logging
from datetime import datetime

from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import station_dao
from recommend import destination, itinerary, pipeline, scoring
from recommend.routing import haversine
from schemas.recommend_schema import (
    Course,
    DestinationPlan,
    RecommendResponse,
    SearchCriteria,
)
from schemas.route_schema import StopoverPlace
from services import route_service
from utils import tour_place

logger = logging.getLogger(__name__)

# 이 거리(km) 이상 떨어지면 '지역 이동'으로 보고 그 날 새 숙소를 잡는다.
_LODGING_MOVE_KM = 25.0

# 도착역 자동 선택 2단계 중 Phase A(거친 시도 선별)에서 정밀 재점수할 후보 수.
_SHORTLIST_N = 5

# 경유역 인근 관광지: '역 근처'로 제한할 조회 반경(m)과 노출 개수.
_VIA_RADIUS_M = 3000
_VIA_PLACES_N = 3
# 자동 경유(도착역만 지정)일 때 최종 노출할 경유 후보 수.
_STOPOVER_N = 3

# 코스에 열차 시각 반영: 관광지 1곳당 소요(관람+이동) 추정 시간(h)과 하루 관광 가능 시간대.
_HOURS_PER_PLACE = 2.5
_DAY_START_HOUR = 9
_DAY_END_HOUR = 21
# 역 ↔ 첫·마지막 관광지 이동/탑승 여유(h). 도착 후 바로 관광·관광 직후 바로 승차하는
# 비현실을 막는다. 출발 버퍼는 이동+탑승 대기라 더 크게. 실제 도시별로 다르니 튜닝 여지 상수.
_ARRIVE_BUFFER_H = 0.5   # 도착역 → 첫 관광지
_DEPART_BUFFER_H = 1.0   # 마지막 관광지 → 귀가역 + 탑승 여유


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

    # 경로별로 그 경로의 도착/출발 시각에 맞춘 코스를 엮어 여정을 만든다.
    routes, note = _fetch_routes(db, origin, dest, criteria)
    itineraries = _itineraries_at(criteria, k, (dest.latitude, dest.longitude), routes)
    if not _has_visits(itineraries):
        note = note or "조건에 맞는 추천지를 찾지 못했습니다(TourAPI 실시간 조회 결과 없음)."
    plan = DestinationPlan(
        destination_station_idx=dest.station_idx,
        destination_name=dest.station_name,
        score=None,
        itineraries=itineraries,
        note=note,
    )
    return RecommendResponse(auto_selected=False, destinations=[plan], note=None)


def _recommend_auto_dest(db, criteria, origin, k) -> RecommendResponse:
    """도착역 미지정 — theme+party 기준으로 도착지 후보 최대 3곳을 골라 각각 코스 1개.

    순위·도착역·코스를 모두 '도착역 주변' 기준으로 일치시키기 위해 2단계로 점수화한다.
      Phase A: 시도(area) 스캔으로 거칠게 상위 N개 후보를 추린다(싸다).
      Phase B: 후보별 도착역 주변을 실측 스캔해 그 분포로 재점수화→top-3. 스캔 결과는 코스 생성에 재사용.
    """
    # 자동 도착지 선택은 '출발역 → 후보 지역' 거리로 점수를 매기므로 출발역 좌표가 필수다.
    # 좌표가 없으면 (0,0)으로 계산돼 거리가 전부 깨지니, 조용히 진행하지 말고 명확히 막는다.
    if origin.latitude is None or origin.longitude is None:
        raise BadRequestException("출발역 좌표가 없어 도착지 자동 추천을 할 수 없습니다.")
    origin_coords = (origin.latitude, origin.longitude)

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
        coarse, criteria.themes, criteria.party, origin_coords, k - 1,  # nights = 일수 - 1
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
        refined, criteria.themes, criteria.party, origin_coords, k - 1,  # nights = 일수 - 1
        max_travel_minutes=criteria.max_travel_minutes, top_k=3,
    )
    if not final:
        return _auto_fallback(db, criteria, origin, origin_coords, k)

    plans = []
    for p in final:
        st = p.station
        # 경로별로 그 경로의 도착/출발 시각에 맞춘 코스를 엮는다. Phase B 장소를 재사용(추가 조회 없음).
        routes, rnote = _fetch_routes(db, origin, st, criteria)
        itineraries = _itineraries_from(
            place_cache[st.station_idx], criteria, k, (st.latitude, st.longitude), routes
        )
        plans.append(DestinationPlan(
            destination_station_idx=st.station_idx,
            destination_name=st.station_name,
            score=round(p.score, 4),
            itineraries=itineraries,
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
    routes, rnote = _fetch_routes(db, origin, st, criteria)
    itineraries = _itineraries_at(criteria, k, (st.latitude, st.longitude), routes)
    plan = DestinationPlan(
        destination_station_idx=st.station_idx,
        destination_name=st.station_name,
        score=None,
        itineraries=itineraries,
        note=rnote or "테마 조건에 맞는 도착지 후보를 찾지 못해 인근 대도시를 추천했습니다.",
    )
    return RecommendResponse(auto_selected=True, destinations=[plan], note=None)


def _itineraries_at(criteria: SearchCriteria, k: int, anchor, routes: list) -> list:
    """현지 기준점(anchor) 반경 추천지를 실시간 조회 후 경로별 여정 생성(지정/폴백용)."""
    places = tour_place.live_places(anchor[0], anchor[1], criteria.themes)
    return _itineraries_from(places, criteria, k, anchor, routes)


def _has_visits(itineraries: list) -> bool:
    """여정 목록에 방문(관광지) 세그먼트가 하나라도 있는지 — '추천지 없음' 안내 판정용."""
    return any(s.kind == "visit" for it in itineraries for s in it.segments)


def _itineraries_from(places, criteria: SearchCriteria, k: int, anchor, routes: list) -> list:
    """이미 받아둔 추천지(places)로 경로별 통합 여정을 만든다.

    점수화·운영시간 조회(네트워크)는 목적지당 한 번만 하고(_prepare_scored), build_courses는
    경로마다 그 경로의 도착/출발 시각(_day_caps/_day_windows)에 맞춰 새로 돌린다(경유는 늦은
    도착이 첫날에 반영됨). 숙소 조회는 memo를 경로 간 공유해 중복 호출을 막는다.

    경로가 없으면(같은 역·조회 실패) 기차 없는 '현지 여행' 여정 하나. 코스가 비면(추천지 없음)
    각 경로는 기차만 있는 여정으로 나온다(경로 정보 보존).
    """
    scored = _prepare_scored(places, criteria, k)
    memo: dict = {}  # 숙소 조회 캐시(경로 간 공유) — 종점 좌표가 같으면 재사용
    if not routes:
        best = _course_for_route(scored, criteria, k, anchor, None, memo)
        return [itinerary.build_itinerary(None, best, criteria.go_date)] if best is not None else []
    return [
        itinerary.build_itinerary(r, _course_for_route(scored, criteria, k, anchor, r, memo), criteria.go_date)
        for r in routes
    ]


def _prepare_scored(places, criteria: SearchCriteria, k: int) -> list:
    """추천지 점수화 + 운영시간 채우기(목적지당 1회, 네트워크). 경로별 build_courses가 공유."""
    scored = scoring.score_places(places, criteria.themes)
    _attach_hours(scored, criteria, k)
    return scored


def _course_for_route(scored: list, criteria: SearchCriteria, k: int, anchor, route, memo: dict) -> Course | None:
    """한 경로의 도착/출발 시각에 맞춰 대표 코스(최고 선호도)를 만든다. 추천지 없으면 None.

    route=None이면 시각 제약 없는 기본 코스(현지 여행). build_courses는 인메모리라 경로마다
    반복해도 싸다. 숙소만 memo 공유로 중복 조회를 막는다.
    """
    first_cap, last_cap = _day_caps(route, k)
    windows = _day_windows(route, k)
    courses = pipeline.build_courses(scored, criteria, k, anchor, first_cap, last_cap, windows)
    if not courses:
        return None
    best = max(courses, key=lambda c: c.total_preference_score)
    _assign_lodgings([best], memo)
    return best


def _attach_hours(scored: list, criteria: SearchCriteria, k: int) -> None:
    """코스에 배정될 상위 후보(작업셋)에 한해 운영시간(오픈/마감·휴무요일)을 채운다.

    detailIntro2는 장소당 1콜이라, 실제 코스 후보가 되는 pipeline.working_set(k)개까지만
    조회해 호출 수를 제한한다(공모전 quota·응답속도 보호). 미상은 그대로 두어 시간 제약 없음으로 본다.
    조회 대상은 반드시 build_courses와 같은 working_set이어야 한다(다중 테마 시 원점수 상위 N개와
    달라 미조회 후보가 코스에 섞이는 것을 방지).
    """
    pool = pipeline.working_set(scored, criteria.themes, k)
    refs = [(str(sp.place_idx), sp.content_type_id) for sp in pool if sp.content_type_id]
    if not refs:
        return
    hours = tour_place.fetch_hours(refs)
    for sp in pool:
        h = hours.get(str(sp.place_idx))
        if h is not None:
            sp.open_hour = h.open_hour
            sp.close_hour = h.close_hour
            sp.closed_weekdays = h.closed_weekdays


def _cap_from_hours(hours: float) -> int:
    """가용 시간(h) → 그 날 관광지 수 상한(하한 0). 상한(_MAX_PER_DAY)은 pipeline이 적용."""
    return max(0, int(hours // _HOURS_PER_PLACE))


def _day_caps(route, k: int) -> tuple[int | None, int | None]:
    """이 경로의 도착/출발 시각으로 (첫날, 마지막날) 관광지 상한을 구한다.

    첫날은 '도착시각~하루 끝', 마지막날은 '하루 시작~출발시각'의 가용 시간으로 제한한다
    (오후 도착이면 첫날을 덜, 오전 귀가면 마지막날을 거의 안 채움). 당일치기(k<=1)는
    도착~출발 사이만. 경유는 go_trains[-1](최종 도착)·back_trains[0](첫 귀가)라 경유 체류가
    반영된다. 경로 없음/시각 못 구하면 (None, None) → pipeline이 기본 상한을 쓴다.
    """
    if route is None or not route.go_trains or not route.back_trains:
        return None, None
    arr = route.go_trains[-1].arr_time    # 도착일 도착 시각
    dep = route.back_trains[0].dep_time   # 귀가일 출발 시각
    # 역↔관광지 이동/탑승 여유를 뺀 실제 관광 가능 시각(도착은 미루고 출발은 당김).
    arr_h = arr.hour + arr.minute / 60 + _ARRIVE_BUFFER_H
    dep_h = dep.hour + dep.minute / 60 - _DEPART_BUFFER_H
    if k <= 1:
        cap = _cap_from_hours((dep - arr).total_seconds() / 3600 - _ARRIVE_BUFFER_H - _DEPART_BUFFER_H)
        return cap, cap
    first = _cap_from_hours(_DAY_END_HOUR - arr_h)
    last = _cap_from_hours(dep_h - _DAY_START_HOUR)
    return first, last


def _day_windows(route, k: int) -> list[tuple[float, float]] | None:
    """이 경로의 도착/출발 시각으로 날짜별 관광 가능 시간대 (start_h, end_h) 목록을 만든다.

    첫날은 열차 도착시각~하루 끝, 마지막날은 하루 시작~열차 출발시각, 중간날은 하루 종일(9~21).
    양 끝에 역↔관광지 이동/탑승 여유(_ARRIVE/_DEPART_BUFFER_H)를 반영해 도착 후 바로 관광·관광
    직후 바로 승차하는 비현실을 막는다. 당일치기(k<=1)는 도착~출발 한 구간. pipeline이 이 시간대
    안에서 관광지 운영시간에 맞춰 방문 시각을 배정한다. 경로 없음/시각 못 구하면 None → 기본(9~21).
    """
    if route is None or not route.go_trains or not route.back_trains:
        return None
    arr = route.go_trains[-1].arr_time
    dep = route.back_trains[0].dep_time
    arr_h = arr.hour + arr.minute / 60 + _ARRIVE_BUFFER_H   # 역→첫 관광지 여유
    dep_h = dep.hour + dep.minute / 60 - _DEPART_BUFFER_H    # 막 관광지→역+탑승 여유
    if k <= 1:
        return [(arr_h, dep_h)]
    return [
        (arr_h if i == 0 else _DAY_START_HOUR, dep_h if i == k - 1 else _DAY_END_HOUR)
        for i in range(k)
    ]


def _assign_lodgings(courses: list[Course], memo: dict | None = None) -> None:
    """코스별로 그 날 '동선의 종점(마지막 방문지)' 근처 숙소를 실시간 조회해 그 날 밤 숙소로 배정.

    숙소는 하루가 끝나는 곳 근처여야 하므로 관광지 평균이 아니라 '마지막 방문지'를 기준으로 잡는다.
    연속 day 종점이 _LODGING_MOVE_KM 이내면 전날 숙소 유지, 그 이상(지역 이동)이면 새 숙소.
    마지막 날(귀가일)은 숙소 없음. 종점 좌표를 반올림 캐시해 중복 호출을 줄인다.
    memo를 넘기면 경로 간(같은 목적지의 여러 경로) 캐시를 공유해 중복 조회를 막는다.
    """
    if memo is None:
        memo = {}

    def near(c: tuple[float, float]):
        key = (round(c[0], 2), round(c[1], 2))
        if key not in memo:
            memo[key] = tour_place.nearest_lodging(c[0], c[1])
        return memo[key]

    for course in courses:
        cur_end: tuple[float, float] | None = None
        cur_lodging = None
        last = len(course.days) - 1
        for i, day in enumerate(course.days):
            if i == last:
                day.lodging = None
                continue
            c = _day_end_coords(day)
            if c is None:
                day.lodging = cur_lodging
                continue
            moved = cur_end is None or haversine(cur_end[0], cur_end[1], c[0], c[1]) > _LODGING_MOVE_KM
            if moved:
                cur_lodging = near(c)
                cur_end = c
            day.lodging = cur_lodging


def _day_end_coords(day) -> tuple[float, float] | None:
    """그 날 동선의 종점(마지막 방문지) 좌표. places는 방문 순서라 뒤에서부터 좌표 있는 곳을 찾는다."""
    for p in reversed(day.places):
        if p.lat is not None and p.lng is not None:
            return (p.lat, p.lng)
    return None


def _fetch_routes(db: Session, origin, dest, criteria: SearchCriteria) -> tuple[list, str | None]:
    """왕복 기차 경로를 붙인다. 같은 역이거나 조회 실패 시 빈 목록 + 안내."""
    if dest.station_idx == origin.station_idx:
        return [], "출발지와 도착지가 같아 기차 구간이 없습니다(현지 여행)."
    try:
        routes = route_service.recommend(
            db, origin.station_idx, dest.station_idx,
            criteria.go_date, criteria.back_date, criteria.go_time, criteria.back_time,
            nail_pass=criteria.use_naeilpass,
            via_station_idx=criteria.via_station_idx,
        )
        routes = _enrich_stopovers(db, routes, criteria)
        return routes, None
    except Exception as e:
        logger.warning("기차 경로 조회 실패: %s", e)
        return [], "기차 경로를 불러오지 못했습니다(코스만 제공)."


def _enrich_stopovers(db: Session, routes, criteria: SearchCriteria) -> list:
    """경유 경로마다 '역 근처' 추천 관광지를 붙이고, 자동 경유는 테마 관련도로 상위만 남긴다.

    - 각 경유역 주변을 역 인근(_VIA_RADIUS_M)으로만 스캔해, 선택 테마와 겹치는 관광지 수를
      '테마 관련도 점수'로 쓰고(스캔 결과는 stopover_places 노출에도 재사용), 역에서 가까운 순
      상위 _VIA_PLACES_N개를 그 경유 경로에 붙인다.
    - 사용자가 경유역을 지정(via_station_idx)했으면 경유 경로는 이미 그 역 하나뿐 → 그대로 유지.
    - 자동 경유(도착역만 지정)면 테마 관련도 내림차순(동점 시 이동시간 오름차순)으로 정렬해
      상위 _STOPOVER_N개만 남기고 나머지 경유는 버린다. 직통/환승(non-경유)은 항상 유지·선두.
    """
    via_routes = [r for r in routes if r.route_type == "경유" and r.via_station_idx is not None]
    if not via_routes:
        return routes

    # 같은 경유역의 가는편/오는편 경로가 관광 스캔·운영시간 조회를 중복 호출하지 않도록 역 idx로 캐시한다.
    # (양방향 경유를 다 만들어도 관광 API 호출은 '유니크 경유역 수'로 유지 — quota 보호.)
    # 캐시엔 운영시간(역 기준, 방향 무관)까지 담고, 방문 시각(visit_time)은 체류 시간대가 방향마다
    # 달라 경로별로 따로 계산한다(가는편·오는편이 같은 StopoverPlace 객체를 공유하지 않도록 매번 새로 만듦).
    scan_cache: dict = {}  # via_station_idx → (테마관련도 점수, 장소 레코드 리스트)

    def _scan(idx: int):
        if idx not in scan_cache:
            st = station_dao.get_by_idx(db, idx)
            if st is None or st.latitude is None or st.longitude is None:
                scan_cache[idx] = (-1, [])  # 좌표 없어 스캔 불가 → 점수 -1로 자동 경유 정렬 맨 뒤로
            else:
                places = tour_place.live_places(st.latitude, st.longitude, criteria.themes, radius_m=_VIA_RADIUS_M)
                places.sort(key=lambda p: haversine(st.latitude, st.longitude, p.lat, p.lng))
                top = places[:_VIA_PLACES_N]
                # 노출할 경유 관광지의 운영시간을 실시간(detailIntro2)으로 조회(역 근처 ≤3곳).
                hours = tour_place.fetch_hours(
                    [(str(p.place_idx), p.content_type_id) for p in top if p.content_type_id]
                )
                recs = [_stopover_record(p, hours.get(str(p.place_idx))) for p in top]
                scan_cache[idx] = (len(places), recs)  # 역 근처 테마 관광지 수 = 테마 관련도
        return scan_cache[idx]

    scored = []  # (route, theme_score)
    for r in via_routes:
        score, recs = _scan(r.via_station_idx)
        start_dt, end_dt = _via_window(r)  # 이 경로의 경유역 체류 시간대(방향에 따라 다름)
        r.stopover_places = [
            StopoverPlace(
                place_idx=rec["place_idx"], name=rec["name"], region=rec["region"],
                lat=rec["lat"], lng=rec["lng"], themes=rec["themes"], image_url=rec["image_url"],
                open_time=rec["open_time"], close_time=rec["close_time"],
                visit_time=_stopover_visit(start_dt, end_dt, rec["open_hour"], rec["close_hour"]),
            )
            for rec in recs
        ]
        scored.append((r, score))

    if criteria.via_station_idx is not None:
        return routes  # 지정 경유: 가는편/오는편 경로 모두 그대로 유지

    # 자동 경유: 테마 관련도↓, 동점 시 이동시간↑ 순으로 상위 N개만 남긴다.
    # id() 집합으로 살릴 경유를 표시하되, 직통/환승(non-경유)은 조건에서 항상 통과시켜 유지·선두.
    scored.sort(key=lambda x: (-x[1], x[0].total_travel_minutes))
    keep = {id(r) for r, _ in scored[:_STOPOVER_N]}
    return [r for r in routes if r.route_type != "경유" or id(r) in keep]


def _stopover_record(p, h) -> dict:
    """LivePlace + 운영시간(Hours|None) → 경유 관광지 캐시 레코드(방향 무관 필드).

    open_hour/close_hour는 방문 시각 계산용(float), open_time/close_time은 노출용(HH:MM).
    """
    oh = h.open_hour if h else None
    ch = h.close_hour if h else None
    return {
        "place_idx": p.place_idx, "name": p.name, "region": p.region,
        "lat": p.lat, "lng": p.lng, "themes": p.themes, "image_url": p.image_url,
        "open_hour": oh, "close_hour": ch,
        "open_time": _hhmm(oh), "close_time": _hhmm(ch),
    }


def _via_window(r) -> tuple:
    """경유 경로의 '경유역 체류 시간대' (도착 dt, 다음 출발 dt)를 뽑는다.

    가는편 경유(go_trains 2편)면 첫 열차 도착~둘째 열차 출발, 오는편 경유(back_trains 2편)면
    첫 열차 도착~둘째 열차 출발. 판별 불가면 (None, None).
    """
    if len(r.go_trains) >= 2:
        return r.go_trains[0].arr_time, r.go_trains[1].dep_time
    if len(r.back_trains) >= 2:
        return r.back_trains[0].arr_time, r.back_trains[1].dep_time
    return None, None


def _stopover_visit(start_dt, end_dt, open_h: float | None, close_h: float | None) -> str | None:
    """경유 체류 시간대 안에서 예상 방문 시각(HH:MM)을 정한다.

    체류 시작(하차) 시각을 기본으로 하되 개점 전이면 개점 시각으로 미룬다. 체류 시간대에 문 여는
    구간이 없으면(출발 전까지 미개점·이미 마감) None. 시각을 못 구하면(start 없음) None.
    """
    if start_dt is None:
        return None
    start_h = start_dt.hour + start_dt.minute / 60
    end_h = (end_dt.hour + end_dt.minute / 60) if end_dt is not None else start_h
    if open_h is not None and open_h >= end_h:   # 출발 시각까지도 개점 전
        return None
    if close_h is not None and close_h <= start_h:  # 하차 시각에 이미 마감
        return None
    visit_h = max(start_h, open_h) if open_h is not None else start_h
    return _hhmm(visit_h)


def _hhmm(hour: float | None) -> str | None:
    """시각(float 시간) → 'HH:MM'. None이면 None. 자정 넘김(≥24)은 다음날 시각으로 표기."""
    if hour is None:
        return None
    total = int(round(hour * 60))
    hh, mm = divmod(total, 60)
    hh %= 24
    return f"{hh:02d}:{mm:02d}"


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
