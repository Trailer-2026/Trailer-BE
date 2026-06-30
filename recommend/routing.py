"""3·4단계: Day 내부 방문 순서(Nearest Neighbor + 2-opt) + 순환 복귀."""

import math

from recommend.types import ScoredPlace


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 간 거리(km)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _dist(a: ScoredPlace, b: ScoredPlace) -> float:
    return haversine(a.lat, a.lng, b.lat, b.lng)


def path_length(order: list[ScoredPlace]) -> float:
    """열린 경로 총 길이(km)."""
    return sum(_dist(order[i], order[i + 1]) for i in range(len(order) - 1))


def nearest_neighbor(
    points: list[ScoredPlace],
    start: ScoredPlace | None = None,
) -> list[ScoredPlace]:
    """그리디 최근접 이웃으로 초기 방문 순서를 만든다. start 미지정 시 첫 노드부터."""
    if len(points) <= 2:
        return list(points)
    remaining = list(points)
    cur = start if start in remaining else remaining[0]
    remaining.remove(cur)
    order = [cur]
    while remaining:
        nxt = min(remaining, key=lambda p: _dist(cur, p))
        remaining.remove(nxt)
        order.append(nxt)
        cur = nxt
    return order


def two_opt(order: list[ScoredPlace]) -> list[ScoredPlace]:
    """2-opt 지역 탐색으로 교차를 제거(소규모는 순식간). 열린 경로 길이 기준."""
    n = len(order)
    if n < 4:
        return order
    best = order[:]
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 2, n):
                a, b = best[i], best[i + 1]
                c = best[j]
                d = best[j + 1] if j + 1 < n else None
                before = _dist(a, b) + (_dist(c, d) if d else 0.0)
                after = _dist(a, c) + (_dist(b, d) if d else 0.0)
                if after + 1e-9 < before:
                    best[i + 1:j + 1] = reversed(best[i + 1:j + 1]) # 모든 간선 쌍 (i, j)를 검사해서 "뒤집으면 짧아지는" 쌍이 있으면 뒤집고, 더 이상 개선이 없을 때까지(while improved) 반복
                    improved = True
    return best

"""
Nearest Neighbor (nearest_neighbor()) — "가장 가까운 곳부터 그리디하게" 초기 순서를 빠르게 만듭니다. 단, 그리디라서 종종 비효율적인 꼬임이 생깁니다.
2-opt (two_opt()) — 그 초기 순서의 꼬임을 다듬어 개선합니다.
"""

def close_cycle(
    order: list[ScoredPlace],
    origin: tuple[float, float],
) -> list[ScoredPlace]:
    """순환 복귀 — 출발지(origin: lat/lng)에서 진입·복귀가 짧도록 경로 방향을 맞춘다.

    열린 day 경로의 시작점이 출발지에 가깝도록 필요 시 뒤집는다(마지막 day에서 origin 복귀 유리).
    """
    if len(order) < 2:
        return order
    olat, olng = origin
    head = haversine(olat, olng, order[0].lat, order[0].lng)
    tail = haversine(olat, olng, order[-1].lat, order[-1].lng)
    if tail < head:
        return list(reversed(order))
    return order

"""
2-opt는 TSP(외판원 문제, Traveling Salesman Problem) 를 빠르게 개선하는 고전적인 지역 탐색(local search) 알고리즘 
이 파일에서는 "하루(Day) 안에서 관광지들을 어떤 순서로 돌면 이동 거리가 가장 짧은가"를 푸는 데 쓰임
"""