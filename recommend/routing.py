"""3·4단계: Day 내부 방문 순서(Nearest Neighbor + 2-opt) + 순환 복귀."""

import math

from recommend.types import ScoredPlace


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 간 대권거리(km). 엔진 전체의 거리 계산 단일 기준(직선 근사, 도로거리 아님)."""
    r = 6371.0  # 지구 반경(km)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _dist(a: ScoredPlace, b: ScoredPlace) -> float:
    return haversine(a.lat, a.lng, b.lat, b.lng)


def hhmm(hour: float | None) -> str | None:
    """시각(float 시간) → 'HH:MM'. None이면 None. 자정 넘김(≥24)은 다음날 시각으로 표기.

    엔진(pipeline)·서비스(recommend_service)가 공유하는 유일한 시각 포맷터.
    """
    if hour is None:
        return None
    total = int(round(hour * 60))
    hh, mm = divmod(total, 60)
    hh %= 24  # 24:00·26:00 등은 00:00·02:00로 표기
    return f"{hh:02d}:{mm:02d}"


def nearest_neighbor(
    points: list[ScoredPlace],
    start: ScoredPlace | None = None,
) -> list[ScoredPlace]:
    """그리디 최근접 이웃으로 초기 방문 순서를 만든다. start 미지정 시 첫 노드부터.

    2곳 이하는 순서가 무의미하므로 그대로 반환. 결과는 2-opt의 시작 해로 쓰인다.
    """
    if len(points) <= 2:
        return list(points)
    remaining = list(points)
    # start가 목록에 없으면 첫 노드를 시작점으로
    cur = start if start in remaining else remaining[0]
    remaining.remove(cur)
    order = [cur]
    while remaining:
        # 남은 곳 중 현재 위치에서 가장 가까운 곳을 계속 이어붙임
        nxt = min(remaining, key=lambda p: _dist(cur, p))
        remaining.remove(nxt)
        order.append(nxt)
        cur = nxt
    return order


def two_opt(order: list[ScoredPlace]) -> list[ScoredPlace]:
    """2-opt 지역 탐색으로 경로 교차를 제거(소규모는 순식간). 열린 경로 길이 기준.

    NN이 만든 초기 순서의 '꼬임'을 다듬는다. 4곳 미만은 뒤집어도 개선 여지가 없어 그대로 반환.
    """
    n = len(order)
    if n < 4:
        return order
    best = order[:]
    improved = True
    while improved:  # 한 번이라도 개선되면 처음부터 다시 훑는다(더 못 줄일 때까지)
        improved = False
        for i in range(n - 1):
            for j in range(i + 2, n):  # i+2부터: 인접 간선끼리는 뒤집어도 변화 없어 건너뜀
                # 간선 (a-b), (c-d)를 끊고 (a-c), (b-d)로 다시 잇는 스왑을 검토
                a, b = best[i], best[i + 1]
                c = best[j]
                d = best[j + 1] if j + 1 < n else None  # j가 끝이면 d 없음(열린 경로라 뒤 간선 미포함)
                before = _dist(a, b) + (_dist(c, d) if d else 0.0)
                after = _dist(a, c) + (_dist(b, d) if d else 0.0)
                # 1e-9: 부동소수 오차로 인한 무한 반복 방지(실질 개선일 때만 채택)
                if after + 1e-9 < before:
                    # i+1..j 구간을 뒤집으면 위 두 간선만 재연결되는 효과
                    best[i + 1:j + 1] = reversed(best[i + 1:j + 1])
                    improved = True
    return best

# Nearest Neighbor(nearest_neighbor) — "가장 가까운 곳부터 그리디하게" 초기 순서를 빠르게 만든다.
# 단, 그리디라서 종종 비효율적인 꼬임이 생긴다. 2-opt(two_opt)가 그 초기 순서의 꼬임을 다듬는다.

def close_cycle(
    order: list[ScoredPlace],
    origin: tuple[float, float],
) -> list[ScoredPlace]:
    """순환 복귀 — 마지막 방문지가 출발지(origin: lat/lng)에 가깝도록 경로 방향을 맞춘다.

    열린 day 경로의 끝점이 출발지에 가깝도록 필요 시 뒤집는다(마지막 day에서 origin 복귀 유리 —
    마지막 방문 뒤 귀가 열차 타러 가는 이동이 짧아진다). 방문 '집합'은 그대로 두고 방향(순서)만
    origin 좌표 기준으로 뒤집을 뿐, 경로 길이는 불변이다.
    """
    if len(order) < 2:
        return order
    olat, olng = origin
    # 양 끝점과 origin의 거리를 비교해, 시작점이 더 가까우면 뒤집어 끝점을 origin 쪽으로 맞춘다
    head = haversine(olat, olng, order[0].lat, order[0].lng)
    tail = haversine(olat, olng, order[-1].lat, order[-1].lng)
    if head < tail:
        return list(reversed(order))
    return order

# 2-opt는 TSP(외판원 문제)를 빠르게 개선하는 고전적인 지역 탐색(local search) 알고리즘.
# 이 파일에서는 "하루(Day) 안에서 관광지를 어떤 순서로 돌면 이동 거리가 가장 짧은가"를 푸는 데 쓴다.