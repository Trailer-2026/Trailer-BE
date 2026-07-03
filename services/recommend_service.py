import logging
from datetime import datetime, timedelta

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
# 당일치기 경유지 1곳당 근사 소요(h) — 순차 방문 시각 배치용. 역 근처 잠깐이라 목적지(2.5h)보다 짧게.
# ponytail: 고정 1.0h 근사. 실측 이동 연동 시 상향 조정.
_VIA_STAY_H = 1.0
# 식당 contentTypeId(음식점). 경유 중엔 식사 1번만(밥 연속 방지).
_MEAL_CT = 39


def _pick_stopover(places: list, n: int) -> list:
    """거리순 places에서 최대 n곳 선택하되 식당(ct=39)은 1곳만 포함한다.

    경유는 잠깐(2~6h) 들르는 것이라 식사는 한 번이면 충분 — 가까운 식당이 여럿이어도 1곳만 넣어
    '밥 먹고 또 밥'(경유 관광의 식당 연속)을 막고 나머지는 관광지로 채운다.
    """
    out, meals = [], 0
    for p in places:
        if len(out) >= n:
            break
        if p.content_type_id == _MEAL_CT:
            if meals >= 1:
                continue
            meals += 1
        out.append(p)
    return out
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
    routes, note = _fetch_routes(db, origin, dest, criteria, k)
    itineraries = _itineraries_at(db, criteria, k, (dest.latitude, dest.longitude), routes)
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
        routes, rnote = _fetch_routes(db, origin, st, criteria, k)
        itineraries = _itineraries_from(
            db, place_cache[st.station_idx], criteria, k, (st.latitude, st.longitude), routes
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
    routes, rnote = _fetch_routes(db, origin, st, criteria, k)
    itineraries = _itineraries_at(db, criteria, k, (st.latitude, st.longitude), routes)
    plan = DestinationPlan(
        destination_station_idx=st.station_idx,
        destination_name=st.station_name,
        score=None,
        itineraries=itineraries,
        note=rnote or "테마 조건에 맞는 도착지 후보를 찾지 못해 인근 대도시를 추천했습니다.",
    )
    return RecommendResponse(auto_selected=True, destinations=[plan], note=None)


def _itineraries_at(db, criteria: SearchCriteria, k: int, anchor, routes: list) -> list:
    """현지 기준점(anchor) 반경 추천지를 실시간 조회 후 경로별 여정 생성(지정/폴백용)."""
    places = tour_place.live_places(anchor[0], anchor[1], criteria.themes)
    return _itineraries_from(db, places, criteria, k, anchor, routes)


def _has_visits(itineraries: list) -> bool:
    """여정 목록에 방문(관광지) 세그먼트가 하나라도 있는지 — '추천지 없음' 안내 판정용."""
    return any(s.kind == "visit" for it in itineraries for s in it.segments)


def _itineraries_from(db, places, criteria: SearchCriteria, k: int, anchor, routes: list) -> list:
    """이미 받아둔 추천지(places)로 경로별 통합 여정을 만든다.

    점수화·운영시간 조회(네트워크)는 목적지당 한 번만 하고(_prepare_scored), build_courses는
    경로마다 그 경로의 도착/출발 시각(_day_caps/_day_windows)에 맞춰 새로 돌린다(경유는 늦은
    도착이 첫날에 반영됨). 숙박 경유(via_nights>=1)는 두 도시로 나눈 _course_for_overnight로
    코스를 만든다(경유역 추천지는 via_cache로 경유역당 1회 조회). 숙소 조회 memo는 경로 간 공유.

    경로가 없으면(같은 역·조회 실패) 기차 없는 '현지 여행' 여정 하나. 코스가 비면(추천지 없음)
    각 경로는 기차만 있는 여정으로 나온다(경로 정보 보존).
    """
    scored = _prepare_scored(places, criteria, k)
    memo: dict = {}       # 숙소 조회 캐시(경로 간 공유) — 종점 좌표가 같으면 재사용
    via_cache: dict = {}  # 경유역 scored 캐시(경유역당 1회 조회)
    if not routes:
        best = _course_for_route(scored, criteria, k, anchor, None, memo)
        return [itinerary.build_itinerary(None, best, criteria.go_date)] if best is not None else []
    out = []
    for r in routes:
        if r.via_nights >= 1:  # 숙박 경유: 경유 도시 + 목적지 두 구간 코스
            course = _course_for_overnight(db, scored, criteria, k, anchor, r, memo, via_cache)
        else:
            course = _course_for_route(scored, criteria, k, anchor, r, memo)
        out.append(itinerary.build_itinerary(r, course, criteria.go_date))
    return out


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


def _caps_arrival_only(arr, k: int) -> tuple[int | None, int | None]:
    """도착일만 제약하고 마지막날은 출발 제약 없는 구간의 (첫날, 마지막날) 상한.

    '먼저 묵는 도시'용 — 마지막날은 그 밤을 자고 다음날 아침에 이동하므로 하루 종일 관광 가능(제약 없음).
    """
    if arr is None:
        return None, None
    arr_h = arr.hour + arr.minute / 60 + _ARRIVE_BUFFER_H
    return _cap_from_hours(_DAY_END_HOUR - arr_h), None


def _windows_arrival_only(arr, k: int) -> list[tuple[float, float]] | None:
    """도착일만 도착시각으로 시작하고 나머지·마지막날은 하루 종일(9~21)인 시간대 목록('먼저 묵는 도시')."""
    if arr is None:
        return None
    arr_h = arr.hour + arr.minute / 60 + _ARRIVE_BUFFER_H
    return [(arr_h if i == 0 else _DAY_START_HOUR, _DAY_END_HOUR) for i in range(k)]


def _overnight_segments(route) -> list:
    """숙박 경유 경로 → 도시 구간 [(is_via, 도착dt, 출발dt)] (도착시각순 = 방문 순서).

    가는편 숙박(go_trains 2편): 경유 먼저(leg1 도착~leg2 출발) → 목적지(leg2 도착~귀가 출발).
    오는편 숙박(back_trains 2편): 목적지 먼저(가는 도착~목적지 출발) → 경유(도착~귀가 출발).
    """
    go, back = route.go_trains, route.back_trains
    if len(go) >= 2:   # 가는편 숙박: 출발→[경유]→(숙박)→목적지
        via = (True, go[0].arr_time, go[1].dep_time)
        dest = (False, go[-1].arr_time, back[0].dep_time)
    else:              # 오는편 숙박: 출발→목적지→[경유]→(숙박)→귀가
        dest = (False, go[0].arr_time, back[0].dep_time)
        via = (True, back[0].arr_time, back[1].dep_time)
    return sorted([via, dest], key=lambda s: s[1])  # 도착 이른 구간이 앞


def _course_for_overnight(db, dest_scored, criteria, k, dest_anchor, route, memo, via_cache) -> Course | None:
    """숙박 경유 경로용 '두 도시' 코스. 먼저 묵는 도시 + 나중 도시 구간을 만들어 병합한다.

    구간 나눔: 먼저 묵는 도시(seg0)는 첫날 도착~여러 날, 마지막날 출발 제약 없음(자고 다음날 이동).
    나중 도시(seg1)는 (도착~귀가) 표준. 일수는 전이 열차 날짜로 가른다(seg0.dep 날짜 - 가는날).
    각 구간을 build_courses로 만들고 day_no·날짜를 1..k로 재부여해 이어붙인 뒤, 좌표 기반
    _assign_lodgings 한 번으로 경유/목적지 각 도시 숙소를 배정한다(마지막 날=귀가일은 없음).
    경유 추천지·운영시간은 여기서 실시간 조회(via_cache로 경유역 간 중복 방지). 실패 시 None.
    """
    if route.via_nights < 1 or route.via_station_idx is None:
        return None
    via_st = station_dao.get_by_idx(db, route.via_station_idx)
    if via_st is None or via_st.latitude is None or via_st.longitude is None:
        return None
    via_anchor = (via_st.latitude, via_st.longitude)

    # 경유역 추천지 점수화+운영시간(경유역당 1회, via_cache 공유). 목적지 scored는 재사용.
    if route.via_station_idx not in via_cache:
        via_places = tour_place.live_places(via_anchor[0], via_anchor[1], criteria.themes)
        via_cache[route.via_station_idx] = _prepare_scored(via_places, criteria, k)
    via_scored = via_cache[route.via_station_idx]

    go = datetime.strptime(criteria.go_date, "%Y%m%d")
    segs = _overnight_segments(route)
    first_days = (segs[0][2].date() - go.date()).days   # 먼저 묵는 도시 일수 = 전이 열차 출발일 - 가는날
    if first_days < 1 or k - first_days < 1:
        return None

    days: list = []
    for idx, (is_via, arr, dep) in enumerate(segs):
        seg_k = first_days if idx == 0 else (k - first_days)
        seg_anchor = via_anchor if is_via else dest_anchor
        seg_scored = via_scored if is_via else dest_scored
        if idx == 0:  # 먼저 묵는 도시: 마지막날 출발 제약 없음(자고 다음날 이동)
            fc, lc = _caps_arrival_only(arr, seg_k)
            win = _windows_arrival_only(arr, seg_k)
        else:         # 나중 도시: 도착~귀가 표준
            fc, lc = _caps_between(arr, dep, seg_k)
            win = _windows_between(arr, dep, seg_k)
        sub = pipeline.build_courses(seg_scored, criteria, seg_k, seg_anchor, fc, lc, win)
        if sub:
            days.extend(max(sub, key=lambda c: c.total_preference_score).days)

    if not days:
        return None
    # 이어붙인 뒤 day_no·날짜를 여행 첫날 기준 1..k로 재부여(구간별 build_courses는 각자 1부터라).
    for i, d in enumerate(days):
        d.day_no = i + 1
        d.date = (go + timedelta(days=i)).strftime("%Y%m%d")
    merged = Course(
        label="숙박경유",
        origin_station_idx=criteria.origin_station_idx,
        days=days,
        total_preference_score=round(sum(p.preference_score for d in days for p in d.places), 4),
        is_round_trip_closed=bool(criteria.round_trip),
        note=None,
    )
    _assign_lodgings([merged], memo)  # 좌표 기반 → 경유일=경유숙소, 목적지일=목적지숙소, 마지막날=없음
    return merged


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


def _caps_between(arr, dep, k: int) -> tuple[int | None, int | None]:
    """도착 dt(arr)~출발 dt(dep) 사이 k일 일정의 (첫날, 마지막날) 관광지 상한.

    첫날은 '도착시각+버퍼~하루 끝', 마지막날은 '하루 시작~출발시각-버퍼'의 가용 시간으로 제한.
    k<=1이면 도착~출발 한 구간. arr/dep가 없으면(None) (None, None) → pipeline 기본 상한.
    """
    if arr is None or dep is None:
        return None, None
    arr_h = arr.hour + arr.minute / 60 + _ARRIVE_BUFFER_H
    dep_h = dep.hour + dep.minute / 60 - _DEPART_BUFFER_H
    if k <= 1:
        cap = _cap_from_hours((dep - arr).total_seconds() / 3600 - _ARRIVE_BUFFER_H - _DEPART_BUFFER_H)
        return cap, cap
    return _cap_from_hours(_DAY_END_HOUR - arr_h), _cap_from_hours(dep_h - _DAY_START_HOUR)


def _windows_between(arr, dep, k: int) -> list[tuple[float, float]] | None:
    """도착 dt(arr)~출발 dt(dep) 사이 k일 일정의 날짜별 관광 가능 시간대 (start_h, end_h) 목록.

    첫날은 도착시각+버퍼~하루 끝, 마지막날은 하루 시작~출발시각-버퍼, 중간날은 하루 종일(9~21).
    k<=1이면 도착~출발 한 구간. arr/dep가 없으면 None → pipeline 기본 시간대(9~21).
    """
    if arr is None or dep is None:
        return None
    arr_h = arr.hour + arr.minute / 60 + _ARRIVE_BUFFER_H
    dep_h = dep.hour + dep.minute / 60 - _DEPART_BUFFER_H
    if k <= 1:
        return [(arr_h, dep_h)]
    return [
        (arr_h if i == 0 else _DAY_START_HOUR, dep_h if i == k - 1 else _DAY_END_HOUR)
        for i in range(k)
    ]


def _route_arr_dep(route):
    """경로의 (목적지 도착 dt, 귀가 출발 dt). 경유는 go_trains[-1]/back_trains[0]라 체류가 반영된다."""
    if route is None or not route.go_trains or not route.back_trains:
        return None, None
    return route.go_trains[-1].arr_time, route.back_trains[0].dep_time


def _day_caps(route, k: int) -> tuple[int | None, int | None]:
    """이 경로의 목적지 도착/귀가 출발 시각으로 (첫날, 마지막날) 관광지 상한을 구한다."""
    arr, dep = _route_arr_dep(route)
    return _caps_between(arr, dep, k)


def _day_windows(route, k: int) -> list[tuple[float, float]] | None:
    """이 경로의 목적지 도착/귀가 출발 시각으로 날짜별 관광 가능 시간대를 만든다."""
    arr, dep = _route_arr_dep(route)
    return _windows_between(arr, dep, k)


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


def _fetch_routes(db: Session, origin, dest, criteria: SearchCriteria, k: int) -> tuple[list, str | None]:
    """왕복 기차 경로를 붙인다. 같은 역이거나 조회 실패 시 빈 목록 + 안내.

    당일치기 경유(via_nights=0)에 더해, 여행이 3일 이상이면(경유 1박 후 목적지에 최소 하루 남음)
    숙박 경유(via_nights=1) 변형도 요청해 합친다. 사용자는 나온 여정 중에서 고른다.
    """
    if dest.station_idx == origin.station_idx:
        return [], "출발지와 도착지가 같아 기차 구간이 없습니다(현지 여행)."
    try:
        routes = route_service.recommend(
            db, origin.station_idx, dest.station_idx,
            criteria.go_date, criteria.back_date, criteria.go_time, criteria.back_time,
            nail_pass=criteria.use_naeilpass,
            via_station_idx=criteria.via_station_idx,
        )
        if k >= 3:  # 경유 1박 후 목적지에 최소 1박 남는 길이 → 숙박 경유 변형도 후보에 추가
            try:
                overnight = route_service.recommend(
                    db, origin.station_idx, dest.station_idx,
                    criteria.go_date, criteria.back_date, criteria.go_time, criteria.back_time,
                    nail_pass=criteria.use_naeilpass,
                    via_station_idx=criteria.via_station_idx, via_nights=1,
                )
                routes += [r for r in overnight if r.via_nights >= 1]  # main·직통 중복 제외, 숙박 경유만
            except Exception as e:  # 숙박 경유 실패해도 당일치기·직통 경로는 유지
                logger.warning("숙박 경유 조회 실패(기본 경로는 유지): %s", e)
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
                top = _pick_stopover(places, _VIA_PLACES_N)
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
        # 숙박 경유(via_nights>=1)는 경유역 관광을 '코스 날'로 넣으므로 stopover_places를 붙이지 않는다.
        # 당일치기 경유만 체류 시간대 안의 역 근처 방문지를 stopover_places로 노출한다.
        if r.via_nights == 0:
            start_dt, end_dt = _via_window(r)  # 이 경로의 경유역 체류 시간대(방향에 따라 다름)
            visits = _stopover_visits(recs, start_dt, end_dt)  # 체류시간 안에서 순차 배치
            # 체류시간 안에 방문 시각을 못 잡는 곳(vt=None: 폐점·축제 등)은 실행 불가라 타임라인에서 제외.
            r.stopover_places = [
                StopoverPlace(
                    place_idx=rec["place_idx"], name=rec["name"], region=rec["region"],
                    lat=rec["lat"], lng=rec["lng"], themes=rec["themes"], image_url=rec["image_url"],
                    open_time=rec["open_time"], close_time=rec["close_time"],
                    visit_time=vt,
                )
                for rec, vt in zip(recs, visits) if vt is not None
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


def _stopover_visits(recs: list, start_dt, end_dt) -> list:
    """경유 체류 시간대 안에서 여러 경유지의 방문 시각을 '순차'로 배치한다(HH:MM 목록).

    한 곳을 보고 다음 곳으로 이동하므로 커서를 _VIA_STAY_H씩 밀며 개점 시각을 존중한다
    (독립 배치 시 문 연 집들이 전부 하차시각에 몰리는 문제 해결). 체류 안에 못 넣는 곳은
    None(폐점 중이거나 출발 전까지 못 들름). start_dt 없으면 전부 None.
    """
    if start_dt is None:
        return [None] * len(recs)
    start_h = start_dt.hour + start_dt.minute / 60
    end_h = (end_dt.hour + end_dt.minute / 60) if end_dt is not None else start_h
    cursor = start_h
    out = []
    for rec in recs:
        oh, ch = rec["open_hour"], rec["close_hour"]
        arrive = max(cursor, oh) if oh is not None else cursor
        # 개점이 출발 후·하차 때 이미 마감·순차상 출발 넘김 → 방문 불가
        if (oh is not None and oh >= end_h) or (ch is not None and ch <= start_h) or arrive >= end_h:
            out.append(None)
            continue
        out.append(_hhmm(arrive))
        cursor = arrive + _VIA_STAY_H  # 다음 경유지는 이만큼 뒤(관람+이동 근사)
    return out


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
