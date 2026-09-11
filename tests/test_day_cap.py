"""하루 상한이 식사를 안 세는지·코스끼리 안 겹치는지 자체 점검 — `python tests/test_day_cap.py`.

네트워크·DB 없이 pipeline.build_courses 만 돌린다. 프레임워크 없음 — 깨지면 assert 로 죽는다.

지키려는 것 둘:
1. FOOD 테마를 섞어도 중간 날은 관광지 3곳 + 식사 2끼다(예전엔 식사 포함 3곳이라 관광지 1곳).
2. 후보가 충분하면 코스 A/B/C 가 장소를 하나도 공유하지 않는다. 작업셋을 관광지·식당 몫으로
   나누고 날짜 묶기를 관광지로만 하는 이유가 이것이다.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAPI_EXPORT", "1")

from core.enums import Theme
from recommend import pipeline, scheduling
from recommend.types import ScoredPlace
from schemas.recommend_schema import SearchCriteria

K = 3  # 2박3일


def _place(idx: int, ct: int, theme: Theme, score: float, lat: float, lng: float) -> ScoredPlace:
    return ScoredPlace(place_idx=idx, name=f"p{idx}", region=None, lat=lat, lng=lng,
                       themes=[theme], score=score, content_type_id=ct)


def _scored() -> list[ScoredPlace]:
    # 관광지 27 + 식당 18 (= 작업셋 정원). 세 동네(위도 35.0/35.2/35.4)에 점수 구간별로 흩어 놓아
    # 점수 인터리브 버킷(A/B/C) 각각이 세 동네를 다 갖게 한다(동네를 i%3 으로 주면 버킷 스트라이드와
    # 맞물려 한 코스의 식당이 전부 한 동네로 몰린다).
    out = []
    for i in range(27):
        out.append(_place(100 + i, 12, Theme.HISTORY, 0.99 - i * 0.005, 35.0 + (i // 9) * 0.2, 129.0 + (i % 9) * 0.01))
    for i in range(18):
        out.append(_place(300 + i, 39, Theme.FOOD, 0.98 - i * 0.005, 35.0 + (i // 6) * 0.2, 129.0 + (i % 6) * 0.01))
    return sorted(out, key=lambda p: p.score, reverse=True)


def test_working_set_splits_quota():
    ws = pipeline.working_set(_scored(), [Theme.HISTORY, Theme.FOOD], K)
    meals = sum(1 for p in ws if p.content_type_id == scheduling._MEAL_CT)
    assert len(ws) - meals == pipeline._NUM_COURSES * K * pipeline._MAX_PER_DAY, len(ws) - meals
    assert meals == pipeline._NUM_COURSES * K * pipeline._MEALS_PER_DAY, meals
    # 식당이 없는 검색은 예전과 같은 27곳
    ws2 = pipeline.working_set([p for p in _scored() if p.content_type_id != 39], [Theme.HISTORY], K)
    assert len(ws2) == 27, len(ws2)


def test_middle_day_has_three_attractions_and_two_meals_no_overlap():
    criteria = SearchCriteria(origin_station_idx=1, go_date="20260501", back_date="20260503",
                              themes=[Theme.HISTORY, Theme.FOOD])
    courses = pipeline.build_courses(_scored(), criteria, K, origin=(35.0, 129.0))
    assert len(courses) == 3, len(courses)
    ids = [{rp.place_idx for d in c.days for rp in d.places} for c in courses]
    shared = sum(len(ids[i] & ids[j]) for i in range(3) for j in range(i + 1, 3))
    assert shared == 0, f"후보가 충분하면 코스끼리 겹치면 안 된다: {shared}"
    for c in courses:
        mid = c.days[1].places  # 중간 날 — 열차 제약이 없는 종일 관광 날
        attrs = [p for p in mid if p.content_type_id != scheduling._MEAL_CT]
        meals = [p for p in mid if p.content_type_id == scheduling._MEAL_CT]
        assert len(attrs) == pipeline._MAX_PER_DAY, f"{c.label} 중간 날 관광지 {len(attrs)}곳"
        assert len(meals) == pipeline._MEALS_PER_DAY, f"{c.label} 중간 날 식사 {len(meals)}끼"


if __name__ == "__main__":
    test_working_set_splits_quota()
    test_middle_day_has_three_attractions_and_two_meals_no_overlap()
    print("day cap OK")
