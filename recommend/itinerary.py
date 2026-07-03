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


def build_itinerary(route, course, go_date: str) -> Itinerary:
    """기차 경로(route)와 대표 코스(course)를 하나의 시간순 여정으로 병합한다.

    route가 None이면 기차 없는 '현지 여행', course가 None이면 기차만 있는 여정.
    go_date(YYYYMMDD)는 날짜→day_no 환산 기준(=여행 첫날).
    """
    go = datetime.strptime(go_date, "%Y%m%d")
    segs: list[ItinerarySegment] = []

    if route is not None:
        _append_train_legs(segs, route.go_trains, route, go)
    if course is not None:
        for day in course.days:
            for p in day.places:
                segs.append(_visit_seg(p, day.date, go))
            if day.lodging is not None:
                segs.append(ItinerarySegment(kind="lodging", day_no=day.day_no, lodging=day.lodging))
    if route is not None:
        _append_train_legs(segs, route.back_trains, route, go)

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


def _append_train_legs(segs: list, trains: list, route, go: datetime) -> None:
    """열차 다리들을 세그먼트로 추가하되, 경유(2편)면 첫 다리 도착 후 경유역 관광을 끼운다."""
    for i, t in enumerate(trains):
        segs.append(ItinerarySegment(
            kind="train", day_no=_day_no(t.dep_time, go),
            start_time=t.dep_time, end_time=t.arr_time, train=t,
        ))
        # 경유 다리(첫 열차 뒤에 둘째 열차가 있음) → 하차 후 그 역 관광을 방문 세그먼트로 편입.
        if i == 0 and len(trains) >= 2 and route.stopover_places:
            d = t.arr_time.strftime("%Y%m%d")
            for sp in route.stopover_places:
                segs.append(_visit_seg(_stopover_to_place(sp), d, go))


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
