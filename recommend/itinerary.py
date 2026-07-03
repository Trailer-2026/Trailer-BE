"""통합 여정 조립 — 이미 계산된 기차 경로(RouteCandidate)와 관광 코스(Course)를
시간순 세그먼트 하나(Itinerary)로 병합한다.

가는 기차 → (경유역 관광) → 목적지 관광·숙소 → 오는 기차 순서로 세그먼트를 쌓는다.
세그먼트는 이미 정렬된 순서로 만들어지므로 별도 정렬은 하지 않는다(엔진이 방문 순서를
보장). 순수 계산(네트워크·DB 비의존): 경로·코스는 이미 채워져 들어온다.

Phase 1: 대표 코스 1개를 각 경로에 그대로 엮는다(코스는 routes[0] 시각 기준으로 계산됨).
경로별 코스 재계산·경유역 1박은 Phase 2/3에서.
"""
from datetime import datetime, timedelta

from schemas.recommend_schema import (
    Itinerary,
    ItinerarySegment,
    RecommendedPlace,
)
from utils.train_api import KST

# 관광지 1곳 점유 시간(h) — scheduling·recommend_service와 같은 가정(방문 종료시각 표기용).
_HOURS_PER_PLACE = 2.5

# 경유역 관광 1곳 점유 시간(h) — recommend_service._VIA_STAY_H와 동기화(역 근처 잠깐이라 목적지보다 짧게).
# 경유 관광 종료시각은 이 값과 '다음 열차 출발' 중 이른 쪽으로 상한한다(열차 출발 후로 새지 않게).
_STOPOVER_HOURS = 1.0


def build_itinerary(route, course, go_date: str) -> Itinerary:
    """기차 경로(route)와 대표 코스(course)를 하나의 시간순 여정으로 병합한다.

    route가 None이면 기차 없는 '현지 여행', course가 None이면 기차만 있는 여정.
    go_date(YYYYMMDD)는 날짜→day_no 환산 기준(=여행 첫날).
    """
    go = datetime.strptime(go_date, "%Y%m%d")
    segs: list[ItinerarySegment] = []

    if route is not None:
        for t in list(route.go_trains) + list(route.back_trains):
            segs.append(ItinerarySegment(
                kind="train", day_no=_day_no(t.dep_time, go),
                start_time=t.dep_time, end_time=t.arr_time, train=t,
            ))
        # 당일치기 경유 관광(하차역 근처). 숙박 경유는 stopover_places가 비어 관광이 코스에 들어있다.
        if route.stopover_places:
            d = _stopover_date(route)
            dwell = _stopover_arrival(route)  # 체류 시작(하차) 시각 — 방문시각 미상 관광의 정렬 기준
            depart = _stopover_departure(route)  # 다음(둘째) 열차 출발 시각 — 방문 종료 상한
            for sp in route.stopover_places:
                seg = _visit_seg(_stopover_to_place(sp), d, go)
                if seg.start_time is None:  # 체류 중 폐점 등으로 방문시각 미상 → 체류 슬롯에 정렬
                    seg.start_time = dwell
                # 경유 관광 종료시각은 (시작+경유 점유) 또는 다음 열차 출발 중 이른 쪽으로 상한.
                # _visit_seg가 목적지용 2.5h로 찍은 end를 여기서 경유 모델(_STOPOVER_HOURS)로 덮어써
                # 열차 출발(예: 12:16)을 넘겨 "일정 충돌"이 나던 문제를 막는다.
                if seg.start_time is not None:
                    end = seg.start_time + timedelta(hours=_STOPOVER_HOURS)
                    seg.end_time = min(end, depart) if depart is not None else end
                segs.append(seg)
    if course is not None:
        for day in course.days:
            for p in day.places:
                segs.append(_visit_seg(p, day.date, go))
            if day.lodging is not None:
                segs.append(ItinerarySegment(kind="lodging", day_no=day.day_no, lodging=day.lodging))

    # 시각순 정렬로 기차·관광·숙소를 하나의 타임라인으로 엮는다(숙박 경유의 leg2가 경유 관광
    # 뒤·목적지 관광 앞에 자연히 놓인다). 숙소는 시각이 없어 그 날 끝으로 정렬.
    segs.sort(key=lambda s: _sort_key(s, go))

    return Itinerary(
        label=route.path if route is not None else "현지 여행",
        route_type=route.route_type if route is not None else "현지",
        via_station_idx=route.via_station_idx if route is not None else None,
        segments=segs,
        total_preference_score=course.total_preference_score if course is not None else 0.0,
        total_travel_minutes=route.total_travel_minutes if route is not None else 0,
        total_fare=route.total_fare if route is not None else None,
        is_round_trip_closed=course.is_round_trip_closed if course is not None else False,
        note=route.note if route is not None else None,
    )


def _sort_key(seg, go: datetime) -> datetime:
    """세그먼트 시간순 정렬 키. 시각 있으면 그 시각, 숙소(시각 없음)는 그 날 끝(23:59)으로."""
    if seg.start_time is not None:
        return seg.start_time
    d = go + timedelta(days=seg.day_no - 1)
    return datetime(d.year, d.month, d.day, 23, 59, tzinfo=KST)


def _stopover_leg(route):
    """당일치기 경유의 하차 다리(2편인 방향의 첫 열차)."""
    trains = route.go_trains if len(route.go_trains) >= 2 else route.back_trains
    return trains[0]


def _stopover_date(route) -> str:
    """당일치기 경유 관광이 일어나는 날(하차역 도착일) YYYYMMDD."""
    return _stopover_leg(route).arr_time.strftime("%Y%m%d")


def _stopover_arrival(route):
    """당일치기 경유 체류 시작(하차) 시각 dt."""
    return _stopover_leg(route).arr_time


def _stopover_departure(route):
    """당일치기 경유의 승차 다리(2편인 방향의 둘째 열차) 출발 시각 dt — 경유 관광 종료 상한."""
    trains = route.go_trains if len(route.go_trains) >= 2 else route.back_trains
    return trains[1].dep_time


def _visit_seg(place: RecommendedPlace, date_ymd: str | None, go: datetime) -> ItinerarySegment:
    st = _visit_dt(date_ymd, place.visit_time)
    end = st + timedelta(hours=_HOURS_PER_PLACE) if st is not None else None
    return ItinerarySegment(
        kind="visit",
        day_no=_day_no(st, go) if st is not None else 1,
        start_time=st, end_time=end, place=place,
    )


def _stopover_to_place(sp) -> RecommendedPlace:
    """경유역 관광지(StopoverPlace)를 방문 세그먼트용 RecommendedPlace로 변환(타입 통일).

    경유지는 선호도 점수·추천이유가 없어 0.0/고정 문구로 채운다(역 근처 접근성 기반 추천).
    """
    return RecommendedPlace(
        place_idx=sp.place_idx, name=sp.name, region=sp.region,
        lat=sp.lat, lng=sp.lng, themes=sp.themes,
        preference_score=0.0, reason="경유역 근처 추천지",
        image_url=sp.image_url,
        open_time=sp.open_time, close_time=sp.close_time, visit_time=sp.visit_time,
    )


def _day_no(dt: datetime, go: datetime) -> int:
    return (dt.date() - go.date()).days + 1


def _visit_dt(date_ymd: str | None, hhmm: str | None) -> datetime | None:
    """방문 날짜(YYYYMMDD) + 시각(HH:MM) → KST datetime. 둘 중 하나라도 없으면 None."""
    if not date_ymd or not hhmm:
        return None
    d = datetime.strptime(date_ymd, "%Y%m%d")
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h % 24, m, tzinfo=KST)


def _selfcheck() -> None:
    """조립기 셀프체크 — 세그먼트 종류·순서·경유 편입을 검증. 실행: python -m recommend.itinerary."""
    from schemas.recommend_schema import Course, DayPlan
    from schemas.route_schema import RouteCandidate, RouteTrain, StopoverPlace

    def train(no, dep_s, arr_s, dh, ah):
        return RouteTrain(
            train_no=no, grade="KTX", dep_station=dep_s, arr_station=arr_s,
            dep_time=datetime(2026, 7, 10, dh, 0, tzinfo=KST),
            arr_time=datetime(2026, 7, 10, ah, 0, tzinfo=KST),
            duration_minutes=(ah - dh) * 60, fare=10000,
        )

    place = RecommendedPlace(
        place_idx=1, name="신사", region="부산", lat=35.1, lng=129.0, themes=[],
        preference_score=0.9, reason="테스트", visit_time="14:00",
    )
    lodging_day = DayPlan(day_no=1, date="20260710", places=[place], lodging=None)
    course = Course(label="A", origin_station_idx=1, days=[lodging_day],
                    total_preference_score=0.9, is_round_trip_closed=False)

    # 경유(가는편 2편) 경로: 첫 다리 후 경유 관광이 끼어야 한다.
    sp = StopoverPlace(place_idx=9, name="경유맛집", region="대전", lat=36.3, lng=127.4,
                       themes=[], visit_time="12:00")
    via = RouteCandidate(
        route_type="경유", path="서울→대전→부산", via_station_idx=5,
        go_trains=[train("1", "서울", "대전", 9, 11), train("2", "대전", "부산", 13, 15)],
        stay_minutes=120, back_trains=[train("3", "부산", "서울", 18, 21)],
        total_travel_minutes=300, total_fare=30000, stopover_places=[sp],
    )
    it = build_itinerary(via, course, "20260710")
    kinds = [s.kind for s in it.segments]
    assert kinds == ["train", "visit", "train", "visit", "train"], kinds
    # 경유 관광은 첫 기차 다리 바로 뒤(둘째 다리 앞)에 와야 한다.
    assert it.segments[1].place.name == "경유맛집"
    assert it.segments[1].day_no == 1
    assert it.total_fare == 30000
    assert it.route_type == "경유"
    # 경유 관광 종료시각은 둘째 열차 출발(13:00)을 넘지 않아야 한다(일정 충돌 방지).
    leg2_dep = via.go_trains[1].dep_time
    assert it.segments[1].end_time <= leg2_dep, (it.segments[1].end_time, leg2_dep)

    # 직통(1편) + 현지 여행(route None) 경계.
    direct = RouteCandidate(
        route_type="직통", path="서울→부산", via_station_idx=None,
        go_trains=[train("1", "서울", "부산", 9, 12)], stay_minutes=None,
        back_trains=[train("2", "부산", "서울", 18, 21)], total_travel_minutes=360, total_fare=20000,
    )
    it2 = build_itinerary(direct, course, "20260710")
    assert [s.kind for s in it2.segments] == ["train", "visit", "train"]
    it3 = build_itinerary(None, course, "20260710")
    assert [s.kind for s in it3.segments] == ["visit"] and it3.route_type == "현지"
    print("itinerary selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
