"""5단계: 제약 반영 — 최대 이동시간(예산)·경유지(필수 노드)·내일로 필터링.

routing 전에 클러스터(=Day) 구성을 다듬는다(트리밍 후 경로 계산이 자연스러움).
"""
from recommend import routing
from recommend.types import Cluster

# 도심+이동 혼합 평균 속도(km/h) — 이동시간(분) 추정용
_SPEED_KMH = 30.0


def _day_travel_minutes(cluster: Cluster) -> float:
    order = routing.nearest_neighbor(cluster.members)
    return routing.path_length(order) / _SPEED_KMH * 60.0


def apply(
    clusters: list[Cluster],
    max_travel_minutes: int | None = None,
    required_place_idxs: list[int] | None = None,
    use_naeilpass: bool = False,
) -> list[Cluster]:
    """제약을 반영해 Day 구성을 조정한다.

    - max_travel_minutes: Day별 내부 이동시간 상한. 초과 시 선호도 낮은 방문지부터 제거
      (필수 경유지는 보존). None이면 트리밍 없음.
    - required_place_idxs: 경유지(필수 방문). 트리밍에서 제외.
    - use_naeilpass: 내일로는 기차 구간(route_service)에 적용되며 추천지 필터에는 영향 없음.
    """
    required = set(required_place_idxs or [])
    if not max_travel_minutes:  # None 또는 0 → 예산 무제한
        return clusters

    for cl in clusters:
        while len(cl.members) > 1 and _day_travel_minutes(cl) > max_travel_minutes:
            removable = [m for m in cl.members if m.place_idx not in required]
            if not removable:
                break
            worst = min(removable, key=lambda m: m.score)
            cl.members.remove(worst)
        cl.centroid = (
            sum(m.lat for m in cl.members) / len(cl.members),
            sum(m.lng for m in cl.members) / len(cl.members),
        ) if cl.members else cl.centroid
    return [cl for cl in clusters if cl.members]
