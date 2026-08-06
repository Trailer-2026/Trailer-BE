"""직통 연결 인덱스 기반 프리페치 필터 자체 점검 — `python tests/test_prefetch_filter.py`.

네트워크·DB 없이 순수 함수만 본다(인덱스는 직접 주입).
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

지키려는 것 하나: 이 필터는 **데우기 판정**이라 틀려도 결과가 바뀌면 안 된다. 그래서 판정 불가
(모르는 역·빈 인덱스)는 반드시 '남긴다'(=평소대로 조회) 쪽으로 기울어야 한다. 이걸 반대로
바꾸면 API 콜은 더 줄고 눈에도 안 띄지만, 멀쩡한 경로에서 기차가 조용히 사라진다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import route_service, train_stop_service


def test_build_links():
    """한 열차의 정차 시퀀스 → 앞→뒤 순서쌍만 (뒤→앞은 아니다)."""
    rows = [
        ("001", 1, "서울"), ("001", 2, "대전"), ("001", 3, "부산"),
        ("002", 1, "용산"), ("002", 2, "익산"),
    ]
    links = train_stop_service._build_links(rows)
    assert ("서울", "대전") in links
    assert ("서울", "부산") in links      # 중간역을 건너뛴 쌍도 직통이다
    assert ("대전", "부산") in links
    assert ("부산", "서울") not in links  # 역방향은 그 열차로 못 간다
    assert ("서울", "익산") not in links  # 서로 다른 열차는 잇지 않는다
    assert len(links) == 3 + 1


def test_warmable_filters_only_known_missing():
    """직통이 없다고 '확인된' 구간만 걸러내고, 판정 불가는 남긴다."""
    train_stop_service.direct_links = lambda: frozenset({("서울", "부산"), ("대전", "부산")})
    pairs = {
        ("N1", "N2", "20260812"),   # 서울→부산: 직통 있음 → 남는다
        ("N2", "N1", "20260814"),   # 부산→서울: 인덱스에 없음 → 걸러진다
        ("N1", "N3", "20260812"),   # 광주송정은 인덱스에 없는 역 → 남는다(판정 불가)
        ("N9", "N2", "20260812"),   # N9는 역명 매핑조차 없음 → 남는다(판정 불가)
    }
    kept = route_service._warmable(pairs, {"N1": "서울", "N2": "부산", "N3": "광주송정"})
    assert ("N1", "N2", "20260812") in kept
    assert ("N2", "N1", "20260814") not in kept
    assert ("N1", "N3", "20260812") in kept, "모르는 역은 보수적으로 남겨야 한다"
    assert ("N9", "N2", "20260812") in kept, "매핑 없는 nat_code도 남겨야 한다"


def test_warmable_is_noop_without_index():
    """인덱스가 비면(최초 기동·DB 장애) 예전과 똑같이 전부 데운다."""
    train_stop_service.direct_links = lambda: frozenset()
    pairs = {("N1", "N2", "20260812"), ("N2", "N1", "20260814")}
    assert route_service._warmable(pairs, {"N1": "서울", "N2": "부산"}) == pairs


if __name__ == "__main__":
    _orig = train_stop_service.direct_links
    try:
        for fn in (
            test_build_links,
            test_warmable_filters_only_known_missing,
            test_warmable_is_noop_without_index,
        ):
            fn()
            print(f"  OK {fn.__name__}")
    finally:
        train_stop_service.direct_links = _orig
    print("prefetch filter selfcheck OK")
