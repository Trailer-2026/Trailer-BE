"""Day 내부 시각 스케줄링 — 관광지 운영시간(오픈/마감·휴무요일)을 고려해 하루 방문지를
고르고 방문 시각을 배정한다.

소프트 제약: 운영시간 밖이라 넣을 수 없는 곳은 제외하고 같은 클러스터의 다음(차순위) 후보로
대체한다. 운영시간 정보가 전혀 없는 날은 시간 제약이 없으므로 기존처럼 '동선(지리)' 기준으로
순서를 정한다(NN+2-opt, 마지막 날은 원점 복귀).

순수 계산 모듈이다(네트워크·DB 비의존). 운영시간은 ScoredPlace에 이미 채워져 들어온다.
비결정 요소 없음(같은 입력 → 같은 스케줄).
"""

from recommend import routing
from recommend.types import ScoredPlace

# 관광지 1곳당 관람+이동 추정 소요(h). recommend_service._HOURS_PER_PLACE와 같은 가정.
_HOURS_PER_PLACE = 2.5
# day_window가 없을 때의 기본 관광 가능 시간대(9~21시).
_DAY_START = 9.0
_DAY_END = 21.0
_EPS = 1e-9


def schedule_day(
    candidates: list[ScoredPlace],
    cap: int,
    window: tuple[float, float] | None,
    weekday: int | None,
    *,
    origin: tuple[float, float] | None = None,
    is_last: bool = False,
) -> list[tuple[ScoredPlace, float]]:
    """cap개 이하의 방문지를 운영시간에 맞춰 고르고 방문 시각을 배정한다.

    candidates: 그 날 클러스터 멤버(점수 내림차순). cap보다 많이 받아 대체 후보로 쓴다.
    cap: 그 날 방문지 상한(열차 시각 기반 first/last cap 포함). 0이면 빈 일정.
    window: (start_h, end_h) 그 날 관광 가능 시간대. None이면 기본(9~21).
    weekday: 그 날 요일(월0~일6) 또는 None. 후보의 휴무요일이면 그 날은 제외(하드).
    반환: [(ScoredPlace, arrive_hour)] 방문 순서대로. 빈 리스트 가능.
    """
    if cap <= 0 or not candidates:
        return []
    start, end = window if window else (_DAY_START, _DAY_END)
    # 그 날 문 닫은 곳은 후보에서 제외(휴무일 방문 불가 — 소프트 제약이라도 이건 하드).
    pool = [c for c in candidates if weekday is None or weekday not in c.closed_weekdays]
    if not pool:
        return []

    # 점수 높은 순으로 채우되, 운영시간 안에서 실제로 방문 가능한 조합만 채택한다.
    # 안 맞는 곳은 건너뛰고 다음(차순위) 후보로 대체 → 소프트 제약.
    selected: list[ScoredPlace] = []
    order: list[tuple[ScoredPlace, float]] = []
    for c in pool:
        if len(selected) >= cap:
            break
        ok, trial_order = _feasible(selected + [c], start, end)
        if ok:
            selected.append(c)
            order = trial_order

    if not selected:
        return []

    # 선택된 곳에 운영시간 정보가 하나도 없으면(전부 미상) 시간 제약이 없는 것 →
    # 기존처럼 동선(지리) 기준으로 순서를 정하고, 방문 시각만 균등 배분해 표기한다.
    if not any(_has_hours(p) for p in selected):
        route = routing.two_opt(routing.nearest_neighbor(selected))
        if is_last and origin is not None:
            route = routing.close_cycle(route, origin)
        return [(p, start + i * _HOURS_PER_PLACE) for i, p in enumerate(route)]

    return order


def _feasible(
    places: list[ScoredPlace], start: float, end: float
) -> tuple[bool, list[tuple[ScoredPlace, float]]]:
    """places를 운영시간 안에서 방문 가능한 순서로 배치 시도(EDF: 마감 이른 곳 먼저).

    가능하면 (True, [(place, arrive)]) 방문 순서, 불가하면 (False, []).
    미상 오픈/마감은 그 날 시간대(start/end)로 대체해 '언제든 방문 가능'으로 본다.
    """
    ordered = sorted(places, key=lambda p: (_close_of(p, end), _open_of(p, start)))
    cursor = start
    out: list[tuple[ScoredPlace, float]] = []
    for p in ordered:
        arrive = max(cursor, _open_of(p, start))
        leave = arrive + _HOURS_PER_PLACE
        if leave > min(_close_of(p, end), end) + _EPS:
            return False, []
        out.append((p, arrive))
        cursor = leave
    return True, out


def _has_hours(p: ScoredPlace) -> bool:
    return p.open_hour is not None or p.close_hour is not None


def _open_of(p: ScoredPlace, default: float) -> float:
    return p.open_hour if p.open_hour is not None else default


def _close_of(p: ScoredPlace, default: float) -> float:
    return p.close_hour if p.close_hour is not None else default
