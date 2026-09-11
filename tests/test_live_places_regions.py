"""좌표 조회가 비어도 법정동 보강이 도는지 자체 점검 — `python tests/test_live_places_regions.py`.

네트워크 없음(TourAPI 호출부를 바꿔치기). 프레임워크 없음 — 깨지면 assert 로 죽는다.

지키려는 것: 보강할 시도 목록을 선택 유형의 좌표 조회 결과에서만 뽑으면, 반경 안 항목이 전부
새 분류(옛 코드 없음)인 곳에서 좌표 조회가 비어 보강이 통째로 안 돈다(실측: 광명역 3km 관광지 7곳 누락).
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAPI_EXPORT", "1")

from core.enums import Theme
from utils import tour_place


def _run(typed: list[dict]):
    """선택 유형 좌표 조회가 typed 를 돌려줄 때 법정동 조회가 불린 (시도, 유형) 목록."""
    asked, typeless = [], []

    def location(lat, lng, radius_m, ct, per_type):
        if ct is None:
            typeless.append(1)
            return [{"lDongRegnCd": "41"}]  # 숙박·음식점처럼 옛 코드가 있는 다른 유형 항목
        return typed

    with patch.object(tour_place, "_location_items", location), \
            patch.object(tour_place, "_ldong_items", lambda r, ct: asked.append((r, ct)) or []):
        tour_place.live_places(37.416, 126.884, [Theme.NATURE], radius_m=3000)
    return asked, typeless


def test_empty_coordinate_query_still_enriches():
    asked, typeless = _run([])
    assert typeless == [1], "좌표 조회가 비었는데 시도를 다시 찾지 않았다"
    assert asked and all(r == "41" for r, _ in asked), f"법정동 보강이 안 돌았다: {asked}"


def test_non_empty_query_adds_no_call():
    asked, typeless = _run([{"lDongRegnCd": "11"}])
    assert not typeless, "시도를 이미 얻었는데 유형 없는 조회를 또 했다(쿼터 낭비)"
    assert asked and all(r == "11" for r, _ in asked), asked


if __name__ == "__main__":
    test_empty_coordinate_query_still_enriches()
    test_non_empty_query_adds_no_call()
    print("live_places regions OK")
