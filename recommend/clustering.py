"""2단계: Day 클러스터링 — 지리 좌표 기반 k-means (k = N박N일의 일수).

순수 k-means는 밀집 지역에 점이 쏠려 어떤 날은 과밀·어떤 날은 텅 비는 불균형을 낳는다.
그래서 k-means로 중심만 잡고, 마지막 배정은 '용량 균형(각 클러스터 ≤ ceil(n/k))'으로 해
하루 방문 수를 고르게 만든다(날짜 균형). 여전히 결정적(랜덤 없음).
"""
import math

from recommend.types import Cluster, ScoredPlace


def _sq(a: tuple[float, float], b: tuple[float, float]) -> float:
    # 위경도 평면상 제곱거리. 
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _centroid(members: list[ScoredPlace]) -> tuple[float, float]:
    n = len(members)
    if n == 0:
        return (0.0, 0.0)
    return (sum(m.lat for m in members) / n, sum(m.lng for m in members) / n)


def kmeans_by_geo(scored: list[ScoredPlace], k: int, max_iter: int = 30) -> list[Cluster]:
    """추천지를 lat/lng 기준 k개 군집으로 묶는다(k=여행 일수).

    소규모(수십개)라 순수 파이썬 Lloyd 반복으로 충분(numpy 불필요).
    초기 중심은 위경도 정렬 후 균등 간격으로 선택(결정적 → 재현 가능한 추천).
    빈 군집은 제거하고 day_no를 1..k로 재부여한다.
    """
    # k를 [1, 장소 수] 범위로 제한 — 장소보다 많은 날을 요구해도 빈 군집만 생기지 않도록.
    k = max(1, min(k, len(scored)))
    if not scored:
        return []

    pts = [(p.lat, p.lng) for p in scored]
    # 결정성 핵심: random 시드 대신 위경도 정렬 후 균등 간격으로 초기 중심 선택
    # → 같은 입력이면 항상 같은 초기값 = 같은 결과(추천 재현성 보장).
    ordered = sorted(range(len(pts)), key=lambda i: (pts[i][0], pts[i][1]))
    step = len(ordered) / k
    centroids = [pts[ordered[min(int(i * step), len(ordered) - 1)]] for i in range(k)]

    assign = [0] * len(scored)
    for _ in range(max_iter):  # Lloyd 반복: 배정 → 중심 재계산
        changed = False
        for i, pt in enumerate(pts):
            # 각 점을 가장 가까운 중심에 배정
            nearest = min(range(k), key=lambda c: _sq(pt, centroids[c]))
            if nearest != assign[i]:
                assign[i] = nearest
                changed = True
        for c in range(k):
            members = [scored[i] for i in range(len(scored)) if assign[i] == c]
            # 빈 군집은 중심을 갱신하지 않고 직전 위치 유지(0,0으로 튀는 것 방지)
            if members:
                centroids[c] = _centroid(members)
        # 배정이 한 점도 안 바뀌면 수렴 → 조기 종료
        if not changed:
            break

    # 용량 균형 재배정 — 위 Lloyd로 잡힌 중심을 그대로 두고, 각 클러스터가 대략 ceil(n/k)개를
    # 넘지 않도록 '가까운-여유있는 중심'에 그리디 배정한다(과밀 날의 초과분이 옆 날로 흘러감).
    assign = _balanced_assign(pts, centroids, k)

    clusters: list[Cluster] = []
    day = 1
    for c in range(k):
        members = [scored[i] for i in range(len(scored)) if assign[i] == c]
        # 빈 군집은 건너뛰고 day_no는 살아남은 군집에만 1..k 연속 부여
        if not members:
            continue
        clusters.append(Cluster(day_no=day, members=members, centroid=_centroid(members)))
        day += 1
    return clusters


def _balanced_assign(pts: list, centroids: list, k: int) -> list[int]:
    """점들을 각 클러스터가 floor(n/k)~ceil(n/k)개를 갖도록 2단계로 배정한다(날짜 균형 + 빈 날 없음).

    1단계: 모든 클러스터 용량을 base=floor(n/k)로 두고 '가까운 순' 그리디 → 각 클러스터 정확히 base개.
    2단계: 남은 n%k개(base로 안 들어간 점)를 아직 base인 최근접 클러스터에 얹어 base+1로.
    n≥k(호출부에서 보장)라 base≥1 → 어떤 클러스터도 텅 비지 않고, 최대도 ceil(n/k)로 묶인다.
    정렬 키 (거리, 점idx, 중심idx)로 완전 결정적.
    """
    n = len(pts)
    base = n // k
    assign = [-1] * n
    counts = [0] * k

    # 1단계: 각 클러스터를 base까지 채운다(용량 base·거리순 그리디 → 모두 정확히 base개).
    for _d, i, c in sorted(
        (_sq(pts[i], centroids[c]), i, c) for i in range(n) for c in range(k)
    ):
        if assign[i] == -1 and counts[c] < base:
            assign[i] = c
            counts[c] += 1

    # 2단계: 남은 점을 아직 base인(여유 있는) 최근접 클러스터에 얹는다 → 그 클러스터만 base+1.
    for i in range(n):
        if assign[i] == -1:
            c = min((cc for cc in range(k) if counts[cc] <= base),
                    key=lambda cc: (_sq(pts[i], centroids[cc]), cc))
            assign[i] = c
            counts[c] += 1
    return assign


def _selfcheck() -> None:
    """클러스터 균형 셀프체크 — 한 곳에 쏠린 점도 날짜별로 과밀 없이 나뉜다."""
    def p(idx, lat, lng):
        return ScoredPlace(place_idx=idx, name=f"p{idx}", region="x", lat=lat, lng=lng,
                           themes=[], score=1.0)

    # 6곳 밀집 + 1곳 멀리 = 7곳, k=3. 순수 k-means면 한 날 6곳 쏠릴 수 있음.
    pts = [p(i, 35.0 + i * 0.001, 129.0) for i in range(6)] + [p(99, 36.2, 127.5)]
    clusters = kmeans_by_geo(pts, k=3)
    sizes = sorted(len(c.members) for c in clusters)
    assert sum(sizes) == 7, sizes
    assert max(sizes) <= math.ceil(7 / 3), f"과밀 없음(≤3): {sizes}"   # 용량 균형 상한
    assert min(sizes) >= 1, f"텅 빈 날 없음: {sizes}"
    # 균형 결과가 결정적(같은 입력 → 같은 크기)인지
    assert sorted(len(c.members) for c in kmeans_by_geo(pts, k=3)) == sizes

    # 얇은 경우 n=4, k=3 — 예전 ceil-cap 방식이면 [2,2,0]로 하루가 사라질 수 있던 케이스.
    thin = [p(i, 35.0, 129.0 + i * 0.001) for i in range(3)] + [p(88, 37.0, 127.0)]
    cs2 = kmeans_by_geo(thin, k=3)
    sz2 = sorted(len(c.members) for c in cs2)
    assert len(cs2) == 3, f"3일 다 살아있어야: {len(cs2)}일"
    assert sz2 == [1, 1, 2], f"floor~ceil 균형: {sz2}"
    print("clustering selfcheck OK:", sizes, "| thin", sz2)


if __name__ == "__main__":
    _selfcheck()
