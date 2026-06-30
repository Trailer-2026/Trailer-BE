"""2단계: Day 클러스터링 — 지리 좌표 기반 k-means (k = N박N일의 일수)."""

from recommend.types import Cluster, ScoredPlace


def _sq(a: tuple[float, float], b: tuple[float, float]) -> float:
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
    k = max(1, min(k, len(scored)))
    if not scored:
        return []

    pts = [(p.lat, p.lng) for p in scored]
    ordered = sorted(range(len(pts)), key=lambda i: (pts[i][0], pts[i][1]))
    step = len(ordered) / k
    centroids = [pts[ordered[min(int(i * step), len(ordered) - 1)]] for i in range(k)]

    assign = [0] * len(scored)
    for _ in range(max_iter):
        changed = False
        for i, pt in enumerate(pts):
            nearest = min(range(k), key=lambda c: _sq(pt, centroids[c]))
            if nearest != assign[i]:
                assign[i] = nearest
                changed = True
        for c in range(k):
            members = [scored[i] for i in range(len(scored)) if assign[i] == c]
            if members:
                centroids[c] = _centroid(members)
        if not changed:
            break

    clusters: list[Cluster] = []
    day = 1
    for c in range(k):
        members = [scored[i] for i in range(len(scored)) if assign[i] == c]
        if not members:
            continue
        clusters.append(Cluster(day_no=day, members=members, centroid=_centroid(members)))
        day += 1
    return clusters
