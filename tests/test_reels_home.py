"""홈 카드용 릴스 지역 태그 자체 점검 — `python tests/test_reels_home.py`.

카카오 호출은 스텁으로 갈음한다(네트워크·REST 키 없이 도는 게 목적).
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
"""
import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import video_service
from utils import kakao_local


def _stub_urlopen(payload):
    """kakao_local 이 쓰는 urlopen 을 캔 응답으로 바꾼다 → 요청 URL 을 기록해 돌려준다."""
    seen = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def fake(req, timeout=None):
        seen.append(req.full_url)
        return _Response(json.dumps(payload).encode())

    urllib.request.urlopen = fake
    kakao_local._rest_key = lambda: "test-key"
    return seen


def main():
    original = urllib.request.urlopen
    try:
        # 1) 시/도 정식명이 짧은 태그로 줄어든다.
        _stub_urlopen({"documents": [{"region_1depth_name": "강원특별자치도"}]})
        assert kakao_local.region_of(37.38, 128.66) == "강원", "시/도 축약 실패"

        # 2) 바다·해외처럼 documents 가 비면 None.
        _stub_urlopen({"documents": []})
        assert kakao_local.region_of(0.0, 0.0) is None, "빈 응답은 None 이어야 한다"

        # 3) 표에 없는 이름은 정식명 그대로.
        _stub_urlopen({"documents": [{"region_1depth_name": "새이름도"}]})
        assert kakao_local.region_of(1.0, 1.0) == "새이름도"

        # 4) 태그는 출발지가 아니라 '가장 멀리 간 지점' 기준이어야 한다.
        #    서울역 출발 → 원주 경유 → 정선 이면 정선 좌표로 조회해야 "강원"이 뜬다.
        seen = _stub_urlopen({"documents": [{"region_1depth_name": "강원특별자치도"}]})
        points = [
            {"latitude": 37.5546, "longitude": 126.9707},  # 서울역(출발)
            {"latitude": 37.3422, "longitude": 127.9202},  # 원주(경유)
            {"latitude": 37.3805, "longitude": 128.6608},  # 정선(최장거리)
        ]
        assert video_service._region_of_trip(points) == "강원"
        assert len(seen) == 1, "지오코딩은 지점당이 아니라 여행당 1회"
        assert "y=37.3805" in seen[0] and "x=128.6608" in seen[0], f"최장거리 지점이 아님: {seen[0]}"

        # 5) 지점이 없거나 카카오가 죽어도 렌더를 막지 않는다(None).
        assert video_service._region_of_trip([]) is None

        def boom(req, timeout=None):
            raise OSError("kakao down")

        urllib.request.urlopen = boom
        assert video_service._region_of_trip(points) is None, "조회 실패는 None 으로 흡수"
    finally:
        urllib.request.urlopen = original

    print("ok")


if __name__ == "__main__":
    main()
