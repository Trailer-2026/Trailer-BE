"""끝난 축제를 코스 후보에서 빼는지 자체 점검 — `python tests/test_festival_period.py`.

네트워크 없이 `tour_place.fetch_hours`만 바꿔치기한다. 프레임워크 없음 — 깨지면 assert 로 죽는다.

지키려는 것 둘:
1. 개최 기간이 여행 기간과 안 겹치는 축제(15)는 `_attach_hours`가 후보에서 뺀다.
2. 빠진 자리에 올라온 차순위도 운영시간을 조회한다(작업셋 = 조회 대상 불변식). 안 지키면
   미조회 후보가 시간 제약 없음으로 코스에 섞인다.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAPI_EXPORT", "1")

from core.enums import Theme
from recommend.types import ScoredPlace
from schemas.recommend_schema import SearchCriteria
from services import recommend_service
from utils.tour_place import Hours


def _sp(idx: int, ct: int, score: float) -> ScoredPlace:
    return ScoredPlace(place_idx=idx, name=f"p{idx}", region=None, lat=35.0, lng=129.0,
                       themes=[Theme.CULTURE], score=score, content_type_id=ct)


def test_runs_between():
    h = Hours(event_start="20260401", event_end="20260430")
    assert h.runs_between("20260429", "20260501")      # 하루라도 겹치면 통과
    assert not h.runs_between("20260501", "20260502")  # 이미 끝난 축제
    assert not h.runs_between("20260301", "20260331")  # 아직 안 시작
    assert Hours().runs_between("20260501", "20260502")  # 기간 미상은 막지 않는다


def test_expired_festival_dropped_and_replacement_fetched():
    # k=1 → 작업셋 9개. 축제(idx 1)가 최상위, idx 10 은 작업셋 밖 차순위.
    scored = [_sp(1, 15, 1.0)] + [_sp(i, 12, 1.0 - i * 0.01) for i in range(2, 11)]
    criteria = SearchCriteria(origin_station_idx=1, go_date="20260501", back_date="20260502",
                              themes=[Theme.CULTURE])
    calls: list[list[str]] = []

    def fake_fetch(refs):
        calls.append([cid for cid, _ in refs])
        out = {cid: Hours(9.0, 18.0) for cid, _ in refs}
        if "1" in out:
            out["1"] = Hours(event_start="20260401", event_end="20260430")  # 4월에 끝난 축제
        return out

    with patch.object(recommend_service.tour_place, "fetch_hours", side_effect=fake_fetch):
        recommend_service._attach_hours(scored, criteria, 1)

    ids = [sp.place_idx for sp in scored]
    assert 1 not in ids, ids
    assert ids == list(range(2, 11)), ids
    # 첫 조회 9건(1~9), 축제가 빠진 뒤 새로 올라온 10 만 추가 조회 — 이미 본 것은 다시 안 부른다.
    assert calls == [[str(i) for i in range(1, 10)], ["10"]], calls
    assert all(sp.open_hour == 9.0 for sp in scored), "차순위(10)까지 운영시간이 채워져야 한다"


if __name__ == "__main__":
    test_runs_between()
    test_expired_festival_dropped_and_replacement_fetched()
    print("festival period OK")
