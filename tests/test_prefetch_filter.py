"""직통 연결 인덱스 기반 프리페치 필터 자체 점검 — `python tests/test_prefetch_filter.py`.

네트워크·DB 없이 순수 함수만 본다(인덱스는 직접 주입).
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

지키려는 것 둘:
- 판정 불가(모르는 역·빈 인덱스)는 반드시 '남긴다'(=평소대로 조회) 쪽으로 기울어야 한다. 이걸
  반대로 바꾸면 API 콜은 더 줄고 눈에도 안 띄지만, 멀쩡한 경로에서 기차가 조용히 사라진다.
- '직통 없음' 판정으로 조회를 **건너뛰는 건 환승 거점 다리뿐**이다. 스냅샷이 하루치라 요일 한정
  열차(부산→마산 금·일 1편)를 모르므로, 경유 후보 다리까지 거르면 그 1편이 사라진다.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# route_service를 import 하면 databases.database가 import 시점에 엔진을 만든다. Postgres 드라이버도
# 설정도 없이 돌 수 있게 인메모리 SQLite로 돌린다(노션 동기화가 쓰는 것과 같은 스위치).
os.environ.setdefault("OPENAPI_EXPORT", "1")

from services import route_service, train_stop_service


class _Db:
    """DAO를 바꿔치기한 테스트용 빈 세션 — close()만 있으면 된다."""

    def close(self):
        pass


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


def test_direct_links_caches_empty_result():
    """빈 테이블이어도 결과는 캐시된다 — 호출마다 전량 조회를 반복하면 안 된다."""
    calls = []

    class _Dao:
        latest_created_at = staticmethod(lambda db: None)
        all_sequences = staticmethod(lambda db: calls.append(db) or [])

    with patch.object(train_stop_service, "SessionLocal", lambda: _Db()), \
            patch.object(train_stop_service, "train_stop_dao", _Dao), \
            patch.object(train_stop_service, "_cache", None):
        assert train_stop_service.direct_links() == (frozenset(), frozenset())
        assert train_stop_service.direct_links() == (frozenset(), frozenset())
        assert len(calls) == 1, "빈 결과도 재사용해야 한다"


def test_direct_links_skips_version_check_within_ttl():
    """TTL 안에서는 적재 시각도 다시 묻지 않는다 — 환승 탐색이 구간마다 부르는 함수라서다."""
    sessions = []

    class _Dao:
        latest_created_at = staticmethod(lambda db: None)
        all_sequences = staticmethod(lambda db: [("001", 1, "서울"), ("001", 2, "부산")])

    with patch.object(train_stop_service, "SessionLocal", lambda: sessions.append(1) or _Db()),             patch.object(train_stop_service, "train_stop_dao", _Dao),             patch.object(train_stop_service, "_cache", None):
        for _ in range(50):
            assert train_stop_service.no_direct("부산", "서울")
        assert len(sessions) == 1, f"DB 세션을 {len(sessions)}번 열었다"


def test_direct_links_fails_open_when_session_broken():
    """세션 생성부터 터져도 빈 집합 — 필터만 꺼지고 추천은 산다. 그 실패도 TTL 동안 기억한다."""
    tries = []

    def _boom():
        tries.append(1)
        raise RuntimeError("no engine")

    with patch.object(train_stop_service, "SessionLocal", _boom), \
            patch.object(train_stop_service, "_cache", None):
        assert train_stop_service.direct_links() == (frozenset(), frozenset())
        for _ in range(50):
            assert not train_stop_service.no_direct("부산", "서울")
        assert len(tries) == 1, f"실패 뒤 DB 연결을 {len(tries)}번 다시 시도했다"


def test_direct_links_keeps_last_index_on_failure():
    """버전 확인이 실패하면 직전 인덱스를 계속 쓴다 — DB가 잠깐 끊겼다고 필터를 끄지 않는다."""
    links = frozenset({("서울", "부산")})
    stale = (0.0, None, links, frozenset({"서울", "부산"}))  # 확인 시각 0 → TTL 만료 상태

    def _boom():
        raise RuntimeError("SSL connection has been closed unexpectedly")

    with patch.object(train_stop_service, "SessionLocal", _boom), \
            patch.object(train_stop_service, "_cache", stale):
        assert train_stop_service.no_direct("부산", "서울"), "직전 인덱스를 버렸다"
        assert train_stop_service._cache[0] > 0, "실패 후 확인 시각을 갱신하지 않았다"


def test_warmable_filters_only_known_missing():
    """직통이 없다고 '확인된' 구간만 걸러내고, 판정 불가는 남긴다."""
    pairs = {
        ("N1", "N2", "20260812"),   # 서울→부산: 직통 있음 → 남는다
        ("N2", "N1", "20260814"),   # 부산→서울: 인덱스에 없음 → 걸러진다
        ("N1", "N3", "20260812"),   # 광주송정은 인덱스에 없는 역 → 남는다(판정 불가)
        ("N9", "N2", "20260812"),   # N9는 역명 매핑조차 없음 → 남는다(판정 불가)
    }
    index = frozenset({("서울", "부산"), ("대전", "부산")})
    known = frozenset(n for p in index for n in p)
    with patch.object(train_stop_service, "direct_links", lambda: (index, known)):
        kept = route_service._warmable(pairs, {"N1": "서울", "N2": "부산", "N3": "광주송정"})
    assert ("N1", "N2", "20260812") in kept
    assert ("N2", "N1", "20260814") not in kept
    assert ("N1", "N3", "20260812") in kept, "모르는 역은 보수적으로 남겨야 한다"
    assert ("N9", "N2", "20260812") in kept, "매핑 없는 nat_code도 남겨야 한다"


def test_warmable_is_noop_without_index():
    """인덱스가 비면(최초 기동·DB 장애) 예전과 똑같이 전부 데운다."""
    pairs = {("N1", "N2", "20260812"), ("N2", "N1", "20260814")}
    with patch.object(train_stop_service, "direct_links", lambda: (frozenset(), frozenset())):
        assert route_service._warmable(pairs, {"N1": "서울", "N2": "부산"}) == pairs


class _St:
    def __init__(self, idx, name, nat):
        self.station_idx, self.station_name, self.nat_code = idx, name, nat


def test_transfer_skips_known_missing_hub_legs():
    """직통 없다고 확인된 거점 다리는 TAGO에 묻지 않는다. 모르는 역 다리는 그대로 묻는다."""
    dep, arr = _St(1, "부산역", "N_BS"), _St(2, "정읍역", "N_JE")
    hub, odd = _St(3, "대전역", "N_DJ"), _St(4, "동탄역", "N_DT")  # 동탄은 train_stop에 없는 SRT역
    index = frozenset({("부산", "대전")})        # 부산→대전만 직통, 대전→정읍은 없음
    known = frozenset({"부산", "대전", "정읍"})
    asked = []

    def legs(dep_nat, arr_nat, ymd, nail_pass=False):
        asked.append((dep_nat, arr_nat))
        return ()

    with patch.object(train_stop_service, "direct_links", lambda: (index, known)),             patch.object(route_service, "_legs", legs):
        route_service._transfer_via_group(dep, arr, [hub, odd], "20260918", None)
    assert ("N_BS", "N_DJ") in asked, "직통 있는 다리는 물어야 한다"
    assert ("N_DJ", "N_JE") not in asked, "직통 없다고 확인된 다리를 물었다"
    assert ("N_BS", "N_DT") in asked, "모르는 역 다리는 물어야 한다"


def test_prefetch_keeps_stopover_legs():
    """경유 후보 다리는 '직통 없음'이어도 데운다 — _direct_missing이 어차피 조회하기 때문이다."""
    dep, arr, stop = _St(1, "부산역", "N_BS"), _St(2, "정읍역", "N_JE"), _St(5, "마산역", "N_MS")
    index = frozenset({("부산", "정읍")})
    known = frozenset({"부산", "정읍", "마산"})  # 부산→마산은 '직통 없음'으로 판정되는 쌍
    warmed = []
    with patch.object(train_stop_service, "direct_links", lambda: (index, known)),             patch.object(route_service, "_safe_fetch", lambda *p: warmed.append(p[:2])),             patch.object(route_service, "_direct_missing", lambda *a: False):
        route_service._prefetch_segments(dep, arr, [], [stop], "20260918", "20260920")
    assert ("N_BS", "N_MS") in warmed, "경유 후보 다리가 프리페치에서 빠졌다"


if __name__ == "__main__":
    # 각 테스트가 patch.object로 자기 뒤처리를 하므로 실행 순서에 의존하지 않는다.
    for fn in (
        test_build_links,
        test_direct_links_caches_empty_result,
        test_direct_links_skips_version_check_within_ttl,
        test_direct_links_fails_open_when_session_broken,
        test_direct_links_keeps_last_index_on_failure,
        test_warmable_filters_only_known_missing,
        test_warmable_is_noop_without_index,
        test_transfer_skips_known_missing_hub_legs,
        test_prefetch_keeps_stopover_legs,
    ):
        fn()
        print(f"  OK {fn.__name__}")
    print("prefetch filter selfcheck OK")
